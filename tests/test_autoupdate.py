"""The station follows the hub's build (issue #58, sizeup/cloud/autoupdate.py): the decisions (the
same build, a different one, unknown, paused, not on main, failed before), the quiet-moment wait,
side-by-side releases (stage + preflight, the switch, the switch back, pruning) with a real git
repository and a fake installer, the station's side (the hub's build -> the want file) and the hub
reporting its own build."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sizeup.cloud import autoupdate as au
from sizeup.cloud.autoupdate import Paths, StepError, Updater, Watch, describe, plan, same_build, wait_quiet

ROOT = Path(__file__).resolve().parent.parent
A = "a" * 40
B = "b" * 40


# -- builds and decisions --------------------------------------------------------------------------------

def test_same_build_matches_a_short_sha_against_the_full_one():
    assert same_build("1a2b3c4", "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b")
    assert same_build("1A2B3C4D5E6F7A8B9C0D1E2F3A4B5C6D7E8F9A0B", "1a2b3c4")
    assert not same_build("1a2b3c4", "1a2b3c5")
    for unknown in ("", None, "dev", "1a2b3c", "not-a-sha", "1a2b3c4 extra"):
        assert not same_build(unknown, unknown) and au.norm(unknown) == ""
    assert au.short(A) == "aaaaaaa" and au.short("") == "?"


def test_plan_equal_differs_unknown_paused_failed():
    assert plan(A, A, "", {})[0] == "current"
    assert plan(A[:7], A, "", {})[0] == "current"
    assert plan(B, A, "", {}) == ("update", "aaaaaaa -> bbbbbbb")
    assert plan("", A, "", {})[0] == "none"                 # the hub's build unknown: nothing
    assert plan("garbage", A, "", {})[0] == "none"
    assert plan(B, A, "auto-update is off", {}) == ("paused", "auto-update is off")
    act, why = plan(B, A, "", {B[:12]: {"at": 1, "why": "/healthz didn't answer"}})
    assert act == "failed-before" and "didn't answer" in why          # never retried ...
    assert plan("c" * 40, A, "", {B: {"at": 1, "why": "x"}})[0] == "update"   # ... until the hub moves on


def test_paused_reasons():
    assert au.paused_reason({"ref": "main", "source": "git", "switch": "on", "config": True}) == ""
    assert "autoupdate on" in au.paused_reason({"switch": "off"})
    assert "auto_update = false" in au.paused_reason({"config": False})
    assert "--ref fix-radio" in au.paused_reason({"ref": "fix-radio"})       # testing a branch: stays put
    assert "folder" in au.paused_reason({"source": "dir"})


def test_wait_quiet_waits_for_a_gap_then_goes_anyway():
    t = [0.0]
    clock, sleep = (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))
    said = iter(["carrier open on FG1", "1 talking", ""])
    assert wait_quiet(lambda: next(said), 300, clock, sleep) == (10.0, "")
    t[0] = 0.0
    waited, why = wait_quiet(lambda: "carrier open on South 2", 300, clock, sleep)
    assert waited == 300 and why == "carrier open on South 2"
    t[0] = 0.0
    assert wait_quiet(lambda: "", 300, clock, sleep) == (0.0, "")


def test_describe_says_up_to_date_updating_failed_paused_behind():
    now = 1000.0
    assert describe(A, A, {}, now)["text"] == "up to date (aaaaaaa)"
    assert describe(A, "", {}, now)["state"] == "unknown"
    d = describe(A, B, {"state": "waiting", "to": B, "since": now - 10}, now)
    assert d["state"] == "updating" and "quiet moment" in d["text"]
    assert describe(A, B, {"state": "updating", "to": B, "since": now - 10}, now)["text"] == "updating aaaaaaa -> bbbbbbb"
    d = describe(A, B, {"state": "failed", "failed": {B: {"at": now, "why": "preflight: imports fail"}}}, now)
    assert d["state"] == "failed" and "update to bbbbbbb failed (preflight: imports fail)" in d["text"]
    assert describe(A, B, {"paused": "auto-update is off"}, now)["state"] == "paused"
    assert describe(A, B, {}, now) == {"state": "behind", "text": "on aaaaaaa, the hub runs bbbbbbb"}
    assert "install-windows.ps1" in describe(A, A, {"windows": au.WINDOWS_STALE}, now)["text"]


# -- the station's side: the hub's build -> /run/sizeup/update-wanted ---------------------------------------

def test_watch_writes_the_hubs_build_once_it_holds_still(tmp_path):
    t = [0.0]
    w = Watch(own=A, run_dir=str(tmp_path), state_path=str(tmp_path / "none.json"), clock=lambda: t[0], log=lambda m: None)
    want = tmp_path / "update-wanted"
    for _ in range(10):                     # a rolling deploy: old and new answer in turn
        w.hub(A)
        t[0] += 2
        w.hub(B)
        t[0] += 2
    assert not want.exists() and w.hub_build == ""
    for _ in range(20):                     # then only the new one
        w.hub(B)
        t[0] += 2
    assert want.read_text() == B + "\n" and w.hub_build == B
    assert w.status()["state"] == "behind"
    m = want.stat().st_mtime_ns
    w.hub(B)
    assert want.stat().st_mtime_ns == m      # not rewritten on every answer (the path unit fires on a write)
    t[0] += au.REFRESH_S
    w.hub(B)
    assert want.read_text() == B + "\n"     # refreshed now and then: a want too old is ignored
    for _ in range(20):                     # the hub says nothing about its build: the want goes
        w.hub("")
        t[0] += 2
    assert not want.exists() and w.hub_build == ""


def test_watch_without_a_run_dir_only_tells(tmp_path):
    w = Watch(own=A, run_dir="", clock=lambda: 1e9, log=lambda m: None)
    w.hub(B)
    w._since = 0
    w.hub(B)
    assert w.hub_build == B and w.status()["text"] == "on aaaaaaa, the hub runs bbbbbbb"


# -- the updater: real git, a fake installer --------------------------------------------------------------

def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main",
                           *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Repo:
    """origin (a work tree on main) and the station's bare clone of it, like /opt/sizeup/repo.git."""

    def __init__(self, tmp: Path):
        self.origin = tmp / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        self.opt = tmp / "opt"
        self.opt.mkdir()
        self.shas = [self.commit("one")]
        subprocess.run(["git", "clone", "-q", "--bare", str(self.origin), str(self.opt / "repo.git")], check=True)
        git(self.opt, "--git-dir=repo.git", "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        git(self.opt, "--git-dir=repo.git", "fetch", "-q", "origin")

    def commit(self, text: str, branch: str = "main") -> str:
        git(self.origin, "checkout", "-q", "-B", branch)
        (self.origin / "f.txt").write_text(text)
        git(self.origin, "add", "f.txt")
        git(self.origin, "commit", "-q", "-m", text)
        sha = git(self.origin, "rev-parse", "HEAD")
        if branch != "main":
            git(self.origin, "checkout", "-q", "main")
        return sha


class FakeUpdater(Updater):
    """The orchestration for real; the installer (stage, activate), /healthz and the station's
    quiet / radio state faked."""

    def __init__(self, repo: Repo, tmp: Path, **kw):
        paths = Paths(opt=repo.opt, want=tmp / "run" / "update-wanted", work=tmp / "work", state=tmp / "state.json",
                      log=tmp / "update.log", install_conf=tmp / "install.conf", config=tmp / "config.toml",
                      windows=tmp / "win")
        (tmp / "run").mkdir(exist_ok=True)
        self.t = [1000.0]
        self.lines: list[str] = []
        super().__init__(paths, clock=lambda: self.t[0], sleep=lambda s: self.t.__setitem__(0, self.t[0] + s),
                         out=self.lines.append, **kw)
        self.calls: list[tuple] = []
        self.bad_preflight: set[str] = set()
        self.unhealthy: set[str] = set()
        self.busy_for = 0                   # polls that still see a carrier open
        self.releases_dir = repo.opt / "releases"
        self.releases_dir.mkdir(exist_ok=True)

    def make_release(self, name: str, build: str, builds: str = "") -> None:
        d = self.releases_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "BUILD").write_text(build + "\n")
        (d / "BUILDS").write_text(builds or f"trunk-recorder={self.p.builds}/trunk-recorder-v1\n")
        (d / ".complete").touch()

    def switch(self, name: str) -> None:
        cur = self.p.current
        tmp = cur.with_name(".current.tmp")
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(f"releases/{name}")
        os.replace(tmp, cur)
        with (self.releases_dir / ".history").open("a") as f:
            f.write(name + "\n")

    def stage(self, sha):
        self.calls.append(("stage", sha))
        if sha in self.bad_preflight:
            raise StepError("preflight failed: imports fail: ModuleNotFoundError: No module named 'nope'")
        self.make_release(sha, sha)

    def activate(self, name, leaving=""):
        self.calls.append(("activate", name))
        self.switch(name)

    def healthy(self, sha):
        return "/healthz didn't answer within 90 s of the switch" if sha in self.unhealthy else ""

    def busy(self):
        if self.busy_for > 0:
            self.busy_for -= 1
            return "carrier open on FG1"
        return ""

    def radios_ok(self):
        return {"South 2", "FG1"}

    def want(self, sha: str) -> None:
        self.p.want.write_text(sha + "\n")


@pytest.fixture
def station(tmp_path):
    repo = Repo(tmp_path)
    u = FakeUpdater(repo, tmp_path)
    u.make_release(repo.shas[0], repo.shas[0])
    u.switch(repo.shas[0])
    return repo, u


def test_the_station_moves_to_the_hubs_build_side_by_side(station):
    repo, u = station
    new = repo.commit("two")
    u.want(new)
    u.busy_for = 3
    assert u.run() == 0
    assert u.current() == (new, new)
    assert u.calls == [("stage", new), ("activate", new)]          # staged first, one switch
    st = u.load()
    assert st["state"] == "current" and st["current"] == new and st["last"]["result"] == "ok"
    assert st["last"]["waited_s"] == 15                             # three polls with a carrier open
    log = (u.p.log).read_text().splitlines()
    assert len(log) == 1 and f"-> {new[:7]}: ok in" in log[0]
    u.calls.clear()
    assert u.run() == 0 and u.calls == []                           # the same build again: nothing
    assert u.previous() == repo.shas[0]                             # a rollback would go back there


def test_nothing_happens_without_a_hub_build_or_when_paused(station):
    repo, u = station
    new = repo.commit("two")
    assert u.run() == 0 and u.calls == []                           # no want: the hub's build unknown
    u.want("unknown")
    assert u.run() == 0 and u.calls == []
    u.want(new)
    os.utime(u.p.want, (time.time() - 2 * au.WANT_MAX_AGE_S,) * 2)
    assert u.run() == 0 and u.calls == []                           # heard too long ago (hub unreachable)
    u.want(new)
    u.p.install_conf.write_text("SIZEUP_REF=main\nSIZEUP_AUTO_UPDATE=off\n")
    u._settings = None
    assert u.run() == 0 and u.calls == []
    assert u.load()["state"] == "paused" and "autoupdate on" in u.load()["paused"]
    u.p.install_conf.write_text("SIZEUP_REF=radio-fix\n")           # a station on a branch stays there
    u._settings = None
    assert u.run() == 0 and u.calls == [] and "--ref radio-fix" in u.load()["paused"]
    u.p.install_conf.write_text("SIZEUP_REF=main\n")
    u.p.config.write_text("[station]\nauto_update = false\n")
    u._settings = None
    assert u.run() == 0 and u.calls == [] and "auto_update = false" in u.load()["paused"]
    u.p.config.write_text("[station]\nauto_update = true\n")
    u._settings = None
    assert u.run() == 0 and u.current()[1] == new                    # on again: goes


def test_never_a_commit_that_isnt_on_main(station):
    repo, u = station
    side = repo.commit("a branch only", branch="feature")
    u.want(side)
    assert u.run() == 1
    assert u.current()[1] == repo.shas[0] and ("stage", side) not in u.calls
    st = u.load()
    assert st["state"] == "failed" and st["last"]["result"] == "refused" and "not on origin/main" in st["last"]["why"]
    u.calls.clear()
    assert u.run() == 0 and u.calls == []                            # not tried again


def test_a_failed_preflight_never_switches(station):
    repo, u = station
    new = repo.commit("two")
    u.bad_preflight.add(new)
    u.want(new)
    assert u.run() == 1
    assert u.current()[1] == repo.shas[0]
    assert u.calls == [("stage", new)]                               # no activate: nothing restarted
    st = u.load()
    assert st["state"] == "failed" and "preflight" in st["failed"][new]["why"]
    assert "not switched" in u.p.log.read_text()


def test_an_unhealthy_release_is_switched_back_and_not_retried_until_the_hub_moves_on(station):
    repo, u = station
    old = repo.shas[0]
    bad = repo.commit("two")
    u.unhealthy.add(bad)
    u.want(bad)
    assert u.run() == 1
    assert u.calls == [("stage", bad), ("activate", bad), ("activate", old)]   # the switch back: nothing rebuilt
    assert u.current() == (old, old)
    st = u.load()
    assert st["state"] == "failed" and st["last"]["result"] == "rolled back" and bad in st["failed"]
    assert "FAILED" in u.p.log.read_text() and "back on" in u.p.log.read_text()
    assert describe(old, bad, u.load())["state"] == "failed"
    u.calls.clear()
    assert u.run() == 0 and u.calls == []                            # the same sha: left alone
    assert u.previous() == ""                                        # a rollback never goes to a failed one
    fixed = repo.commit("three")                                     # the hub moves on
    u.want(fixed)
    assert u.run() == 0 and u.current()[1] == fixed


def test_an_interrupted_update_keeps_a_healthy_release_and_leaves_a_sick_one(station):
    repo, u = station
    old, new = repo.shas[0], repo.commit("two")
    u.make_release(new, new)
    u.switch(new)                                                    # switched, then the box rebooted
    u.save({}, state="updating", to=new, **{"from": old})
    u.run()
    assert u.current()[1] == new and u.load()["state"] == "current"
    u.unhealthy.add(new)
    u.save(u.load(), state="updating", to=new, **{"from": old})
    u.run()
    assert u.current()[1] == old and new in u.load()["failed"]


def test_prune_keeps_three_releases_and_the_builds_they_use(station):
    repo, u = station
    builds = u.p.builds
    for name in ("trunk-recorder-v1", "trunk-recorder-v2", "whisper-old"):
        (builds / name).mkdir(parents=True)
    names = [repo.shas[0]]
    for i in range(4):
        sha = repo.commit(f"r{i}")
        u.make_release(sha, sha, f"trunk-recorder={builds}/trunk-recorder-v{1 if i < 3 else 2}\n")
        u.switch(sha)
        names.append(sha)
    u.make_release("dir-old", "")                                     # never switched to
    u.p.releases.joinpath(".staging-x").mkdir()
    (u.p.opt / "trunk-recorder").symlink_to(builds / "trunk-recorder-v2")
    u.prune()
    assert u.releases() == sorted(names[-3:])
    assert sorted(d.name for d in builds.iterdir()) == ["trunk-recorder-v1", "trunk-recorder-v2"]
    assert not (u.p.releases / ".staging-x").exists()
    assert u.previous() == names[-2]


def test_prune_removes_the_old_checkout_once_its_release_is_gone(station):
    repo, u = station
    (u.p.opt / "app").mkdir()
    (u.p.opt / "venv").mkdir()
    (u.p.releases / ".legacy").write_text(repo.shas[0] + "\n")
    u.prune()
    assert (u.p.opt / "app").is_dir()                                # still the rollback target
    for i in range(3):
        sha = repo.commit(f"r{i}")
        u.make_release(sha, sha)
        u.switch(sha)
    u.prune()
    assert repo.shas[0] not in u.releases()
    assert not (u.p.opt / "app").exists() and not (u.p.opt / "venv").exists()


def test_rollback_by_hand_goes_back_and_holds(station):
    repo, u = station
    old, new = repo.shas[0], repo.commit("two")
    u.want(new)
    u.run()
    assert u.rollback() == 0
    assert u.current()[1] == old and new in u.load()["failed"]
    u.calls.clear()
    assert u.run() == 0 and u.calls == []                            # not straight back to it


def test_a_fetch_that_fails_changes_nothing(station):
    repo, u = station
    new = repo.commit("two")
    u.want(new)
    git(repo.opt, "--git-dir=repo.git", "remote", "set-url", "origin", str(repo.opt / "gone"))
    assert u.run() == 1
    assert u.current()[1] == repo.shas[0] and u.load()["state"] == "behind" and not u.load().get("failed")


def test_windows_scripts_are_refreshed_or_reported(station, monkeypatch):
    repo, u = station
    rel = u.p.current / "deploy" / "station" / "windows"
    rel.mkdir(parents=True)
    (rel / "usb-attach.ps1").write_text("new\n")
    win = u.p.windows
    win.mkdir()
    (win / "usb-attach.ps1").write_text("old\r\n")
    monkeypatch.setattr(u, "is_wsl", lambda: True)
    assert u.windows() == "" and (win / "usb-attach.ps1").read_text() == "new\n"
    (win / "usb-attach.ps1").write_text("new\r\n")                   # CRLF from Windows: the same script
    assert u.windows() == ""
    (win / "usb-attach.ps1").write_text("old\n")
    monkeypatch.setattr(au.shutil, "copyfile", lambda *a: (_ for _ in ()).throw(PermissionError("read-only")))
    assert u.windows().startswith(au.WINDOWS_STALE)


def test_on_and_off_edit_install_conf(tmp_path, monkeypatch):
    conf = tmp_path / "install.conf"
    conf.write_text("# by the installer\nSIZEUP_REF=main\nSIZEUP_MODEL=small.en-q5_1\n")
    monkeypatch.setattr(au, "Paths", lambda: Paths(install_conf=conf, config=tmp_path / "none.toml"))
    assert au.main(["off"]) == 0
    assert "SIZEUP_AUTO_UPDATE=off" in conf.read_text() and "SIZEUP_MODEL=small.en-q5_1" in conf.read_text()
    assert au.main(["on"]) == 0 and "SIZEUP_AUTO_UPDATE=on" in conf.read_text()
    conf.write_text("SIZEUP_REF=radio-fix\n")
    assert au.main(["on"]) == 2                                       # follow the hub from a branch: no


def test_the_updater_is_stdlib_only_and_runs_on_python_311():
    """It runs as /usr/local/lib/sizeup/autoupdate.py with the system python3 (Debian 12: 3.11)."""
    src = (ROOT / "sizeup" / "cloud" / "autoupdate.py").read_text()
    tree = ast.parse(src, feature_version=(3, 11))
    for node in ast.walk(tree):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
            [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
        for name in names:
            assert name.split(".")[0] in sys.stdlib_module_names or name == "__future__", name
    out = subprocess.run([sys.executable, "-I", str(ROOT / "sizeup" / "cloud" / "autoupdate.py"), "--help"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and "rollback" in out.stdout


# -- the hub tells its build; the station passes it on ------------------------------------------------------

def test_the_hub_reports_its_build_and_shows_the_stations_version(tmp_path, monkeypatch):
    from tests.test_cloud import HubRun, call, hub_cfg, station as run_station, station_cfg
    from tests.test_app import wait
    monkeypatch.setenv("SIZEUP_BUILD", B)
    hub = HubRun(hub_cfg(tmp_path))
    monkeypatch.setattr(au, "STABLE_S", 0.0)
    monkeypatch.setenv("SIZEUP_BUILD", A[:7])                         # the station runs an older one
    monkeypatch.setenv("SIZEUP_UPDATE_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    st = run_station(station_cfg(tmp_path, hub.url), {})
    try:
        code, r = call(hub.url + "/api/station/state", "POST", {"at": time.time()})
        assert code == 200 and r["build"] == B and hub.app.hub.build == B
        want = tmp_path / "run" / "update-wanted"
        assert wait(lambda: want.is_file() and want.read_text() == B + "\n", 15)
        assert wait(lambda: "the hub runs bbbbbbb" in hub.app.hub.station_row()["detail"], 15)
        assert "on aaaaaaa" in hub.app.hub.station_row()["detail"]
        assert "version: on aaaaaaa, the hub runs bbbbbbb" in st.link.status_row()["detail"]
    finally:
        st.link.stop()
        hub.close()


def test_a_hub_without_a_build_asks_for_nothing(tmp_path, monkeypatch):
    from tests.test_cloud import HubRun, hub_cfg, station as run_station, station_cfg
    from tests.test_app import wait
    monkeypatch.delenv("SIZEUP_BUILD", raising=False)
    hub = HubRun(hub_cfg(tmp_path))
    monkeypatch.setattr(au, "STABLE_S", 0.0)
    monkeypatch.setenv("SIZEUP_UPDATE_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "update-wanted").write_text(A + "\n")        # from before
    st = run_station(station_cfg(tmp_path, hub.url), {})
    try:
        assert wait(lambda: not (tmp_path / "run" / "update-wanted").exists(), 15)
        assert "the hub's build isn't known" in st.link.status_row()["detail"]
    finally:
        st.link.stop()
        hub.close()


# -- the quiet moment, as the station sees it ---------------------------------------------------------------

def test_station_busy_names_what_a_restart_would_cut_off():
    from sizeup.cloud.stationmode import StationMode
    from sizeup.stt import Transcriber
    stt = Transcriber(SimpleNamespace(), lambda item: None)
    app = SimpleNamespace(radio_status=lambda: [{"label": "FG1", "open": True}, {"label": "South 2", "open": False}],
                          live=SimpleNamespace(state=lambda: [{"rig": "E63"}]), stt=stt, _rerun_ids=set())
    sm = SimpleNamespace(app=app)
    assert StationMode.busy(sm) == "carrier open on FG1, 1 talking"
    import numpy as np
    stt.submit(object(), np.zeros(10, np.float32))
    app.radio_status, app.live = (lambda: []), SimpleNamespace(state=lambda: [])
    assert StationMode.busy(sm) == "1 transcription(s) in flight"
    stt._q.get_nowait()
    assert StationMode.busy(sm) == ""


def test_whisper_service_only_waits_for_an_external_server(monkeypatch):
    from sizeup import app as app_mod
    monkeypatch.setenv("SIZEUP_STT_EXTERNAL", "1")
    started = []
    monkeypatch.setattr(app_mod.subprocess, "Popen", lambda *a, **k: started.append(a))
    w = app_mod.WhisperService("whisper-server", "m.bin", 1, 2)
    w.start()
    from tests.test_app import wait
    assert wait(lambda: "sizeup-whisper.service" in w.detail, 5)
    w.stop()
    assert started == [] and w.state == "starting"


def test_station_config_auto_update(tmp_path):
    from sizeup.config import ConfigError, load
    p = tmp_path / "c.toml"
    p.write_text('[station]\nid = "t"\n')
    assert load(p).station_auto_update is True
    p.write_text('[station]\nauto_update = false\n')
    assert load(p).station_auto_update is False
    p.write_text('[station]\nauto_update = "no"\n')
    with pytest.raises(ConfigError):
        load(p)


def test_state_json_is_what_the_station_reads(station):
    repo, u = station
    new = repo.commit("two")
    u.want(new)
    u.run()
    w = Watch(own=new, run_dir="", state_path=str(u.p.state), log=lambda m: None)
    v = w.status()
    assert v["update"]["state"] == "current" and v["update"]["last"]["result"] == "ok"
    assert json.loads(u.p.state.read_text())["current"] == new
