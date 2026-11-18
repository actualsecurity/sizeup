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
from sizeup.cloud.autoupdate import Paths, StepError, Updater, Watch, describe, hold_of, moment, plan, same_build

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


def test_moment_settles_then_waits_for_quiet_then_a_short_queue():
    quiet = {"hard": [], "stt": 0}
    # a hub build 3 min old: settling (10 min), whatever the station says
    assert moment(180, 0, 0, quiet)[0] == "settle"
    assert moment(600, 0, 0, quiet) == ("", "a quiet moment")
    # a page or a carrier: held, with no limit before the ceiling (45 min behind)
    page = {"hard": ["page on South 2"], "stt": 0}
    assert moment(2000, 0, 0, page) == ("busy", "page on South 2")
    # the queue: 5 at first, 20 after 10 min of waiting, 60 after 20 min (counted from when it settled)
    assert moment(600, 0, 0, {"hard": [], "stt": 6}) == ("stt", "6 transcription(s) waiting: switching at 5 or fewer")
    assert moment(1199, 0, 0, {"hard": [], "stt": 19})[0] == "stt"
    assert moment(1200, 0, 0, {"hard": [], "stt": 19}) == ("", "a quiet moment, 19 transcription(s) waiting")
    assert moment(1800, 0, 0, {"hard": [], "stt": 61})[0] == "stt"
    assert moment(1800, 0, 0, {"hard": [], "stt": 60})[0] == ""


def test_moment_has_a_ceiling_and_an_urgent_path():
    busy = {"hard": ["carrier open on FG1"], "stt": 400}
    # behind 45 min (the hub kept deploying: seen just now): no settling, no queue; what's on the air 5 min more at most
    assert moment(2700, 2690, 0, {"hard": [], "stt": 400}) == (
        "", "45 min behind the hub (the most it waits: 45 min), 400 transcription(s) waiting")
    assert moment(2700, 2690, 0, busy) == ("busy", "carrier open on FG1")
    assert moment(2999, 2690, 0, busy)[0] == "busy"
    ph, why = moment(3000, 2690, 0, busy)
    assert ph == "" and why == "still carrier open on FG1 after 5 min: switching anyway"
    # urgent: no settling, no queue; on the air 5 min at most from when it became urgent
    assert moment(10, 5, 0, {"hard": [], "stt": 400}, urgent_at=5) == ("", "urgent, 400 transcription(s) waiting")
    assert moment(200, 5, 0, busy, urgent_at=5)[0] == "busy"
    assert moment(305, 5, 0, busy, urgent_at=5)[0] == ""


def test_hold_of_reads_the_station_and_an_older_one():
    assert hold_of({"busy": "page on South 2, 3 transcription(s) in flight",
                    "hold": {"hard": ["page on South 2"], "stt": 3}}) == {
        "hard": ["page on South 2"], "stt": 3, "text": "page on South 2, 3 transcription(s) in flight"}
    # a station from before "hold": all of what it says holds, as before
    assert hold_of({"busy": "1 transcription(s) in flight"}) == {
        "hard": ["1 transcription(s) in flight"], "stt": 0, "text": "1 transcription(s) in flight"}
    assert hold_of(None) == {"hard": [], "stt": 0, "text": ""}                # it doesn't answer: quiet
    assert hold_of({"hold": {"hard": "x", "stt": True}})["stt"] == 0          # (nonsense: nothing)


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

    def commit(self, text: str, branch: str = "main", path: str = "f.txt") -> str:
        git(self.origin, "checkout", "-q", "-B", branch)
        (self.origin / path).parent.mkdir(parents=True, exist_ok=True)
        (self.origin / path).write_text(text)
        git(self.origin, "add", path)
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
        self.on_sleep = None                # tests: called with the fake time after each sleep (a deploy, a page)
        kw.setdefault("settle_s", 0.0)      # (the tests before coalescing: the hub's build counts as settled)
        super().__init__(paths, clock=lambda: self.t[0], sleep=self._sleep, out=self.lines.append, **kw)
        self.calls: list[tuple] = []
        self.bad_preflight: set[str] = set()
        self.stage_errors: list[str] = []       # transient: apt lock, network, disk
        self.unhealthy: set[str] = set()
        self.busy_for = 0                   # polls that still see a carrier open
        self.stt = 0                        # transcriptions waiting on the station
        self.holds: list[dict] = []         # what each hold() said
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
            raise StepError("preflight failed: imports fail: ModuleNotFoundError: No module named 'nope'",
                            au.PREFLIGHT_EXIT)
        if self.stage_errors:
            raise StepError(self.stage_errors.pop(0))
        self.make_release(sha, sha)

    def activate(self, name, leaving=""):
        self.calls.append(("activate", name))
        self.switch(name)

    def healthy(self, sha):
        return "/healthz didn't answer within 90 s of the switch" if sha in self.unhealthy else ""

    def _sleep(self, s):
        self.t[0] += s
        if self.on_sleep is not None:
            self.on_sleep(self.t[0])

    def hold(self):
        hard = []
        if self.busy_for > 0:
            self.busy_for -= 1
            hard.append("carrier open on FG1")
        h = {"hard": hard, "stt": self.stt,
             "text": ", ".join(hard + ([f"{self.stt} transcription(s) in flight"] if self.stt else []))}
        self.holds.append(h)
        return h

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
    assert ": ok in " in st["last"]["line"]                          # the log line, for the deploy check (#79)
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
    assert st["state"] == "failed" and st["last"]["result"] == "refused"
    assert st["last"]["why"] in ("not on origin/main", "no such commit in the repository")   # only main is fetched
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
    assert "FAILED" in st["last"]["line"] and "back on" in st["last"]["line"]
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

def _sm(app):
    from sizeup.cloud.stationmode import StationMode
    return SimpleNamespace(app=app), StationMode


def test_station_busy_names_what_a_restart_would_cut_off():
    from sizeup.stt import Transcriber
    stt = Transcriber(SimpleNamespace(), lambda item: None)
    app = SimpleNamespace(radio_status=lambda: [{"label": "FG1", "open": True}, {"label": "South 2", "open": False}],
                          live=SimpleNamespace(state=lambda: [{"rig": "E63"}]), stt=stt, _rerun_ids=set())
    sm, StationMode = _sm(app)
    assert StationMode.busy(sm) == "carrier open on FG1, 1 talking"
    import numpy as np
    stt.submit(object(), np.zeros(10, np.float32))
    app.radio_status, app.live = (lambda: []), SimpleNamespace(state=lambda: [])
    assert StationMode.busy(sm) == "1 transcription(s) in flight"
    assert StationMode.update_hold(sm) == {"busy": "1 transcription(s) in flight", "hold": {"hard": [], "stt": 1}}
    stt._q.get_nowait()
    assert StationMode.busy(sm) == ""


def test_station_hold_never_restarts_mid_page_or_dispatch(tmp_path):
    import sqlite3
    import numpy as np
    from sizeup.stt import URGENT, Transcriber
    stt = Transcriber(SimpleNamespace(), lambda item: None)
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE incidents (id INTEGER PRIMARY KEY, emergency INT, status TEXT, created_at REAL)")

    def one(sql, args=()):
        r = db.execute(sql, args).fetchone()
        return dict(r) if r else None
    windows = []
    app = SimpleNamespace(radio_status=lambda: [], live=SimpleNamespace(state=lambda: []), stt=stt, _rerun_ids=set(),
                          paging_state=lambda: [{"channel": "South 2", "ended": False}, {"channel": "FG1", "ended": True}],
                          pagehold=SimpleNamespace(held={"South 2": (None, 0.0)}),
                          cad=SimpleNamespace(store=SimpleNamespace(one=one),
                                              alarms=SimpleNamespace(windows=SimpleNamespace(state=lambda now: windows))))
    sm, StationMode = _sm(app)
    assert StationMode.hold(sm)["hard"] == ["page on South 2"]          # going out now, or held for its continuation
    app.paging_state, app.pagehold = (lambda: []), SimpleNamespace(held={})
    db.execute("INSERT INTO incidents VALUES (1, 1, 'pending', ?)", (time.time() - 130,))
    db.execute("INSERT INTO incidents VALUES (2, 0, 'pending', ?)", (time.time(),))       # not an emergency: no
    windows.append("tg 1")
    stt.submit(object(), np.zeros(10, np.float32), priority=URGENT)
    stt.submit(object(), np.zeros(10, np.float32))
    h = StationMode.hold(sm)
    assert h["hard"] == ["event of ours dispatched 2 min ago", "emergency window open",
                         "1 page / Fire-EMS clip(s) waiting for speech-to-text"] and h["stt"] == 2
    db.execute("UPDATE incidents SET created_at = ?", (time.time() - 400,))              # dispatched a while ago
    windows.clear()
    stt._q.withdraw(URGENT)
    assert StationMode.hold(sm) == {"hard": [], "stt": 1, "said": ["1 transcription(s) in flight"]}
    app.cad = None                                                                      # no CAD here: nothing
    assert StationMode.hold(sm)["hard"] == []


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


# -- review fixes ------------------------------------------------------------------------------------------

def test_a_transient_build_failure_is_retried_with_backoff_then_given_up(station):
    repo, u = station
    new = repo.commit("two")
    u.want(new)
    u.stage_errors = ["E: Could not get lock /var/lib/dpkg/lock-frontend"] * 5
    assert u.run() == 1
    st = u.load()
    assert st["state"] == "behind" and st["retry"][new]["n"] == 1 and not st.get("failed")
    assert describe(repo.shas[0], new, Watch(own="", run_dir="", state_path=str(u.p.state)).state())["state"] == "updating"
    u.calls.clear()
    assert u.run() == 0 and u.calls == []                            # not before its time
    for n, wait in zip(range(2, 6), (300, 900, 3600, 3600)):
        u.t[0] += wait
        u.run()
        assert u.load().get("retry", {}).get(new, {}).get("n", n) == n
    st = u.load()
    assert st["state"] == "failed" and "couldn't build it 5 times" in st["failed"][new]["why"]
    assert len([c for c in u.calls if c[0] == "stage"]) == 4
    assert u.current()[1] == repo.shas[0]


def test_a_retry_that_works_switches(station):
    repo, u = station
    new = repo.commit("two")
    u.want(new)
    u.stage_errors = ["network unreachable"]
    u.run()
    u.t[0] += 301
    assert u.run() == 0 and u.current()[1] == new and "retry" not in u.load()


def test_docs_only_changes_dont_restart(station):
    repo, u = station
    old = repo.shas[0]
    docs = repo.commit("words", path="docs/cloud.md")
    repo.commit("more words", path="deploy/station/README.md")
    ci = repo.commit("ci", path=".github/workflows/x.yml")
    u.want(ci)
    assert u.run() == 0
    assert u.calls == [] and u.current()[1] == old                   # nothing staged, nothing restarted
    st = u.load()
    assert st["state"] == "current" and st["equivalent"][ci] == old
    assert describe(old, ci, Watch(own="", run_dir="", state_path=str(u.p.state)).state())["text"] == \
        f"up to date: {ci[:7]} (no runtime change since {old[:7]})"
    assert u.run() == 0 and u.calls == []                            # and stays that way: no loop
    code = repo.commit("code", path="sizeup/x.py")
    u.want(code)
    assert u.run() == 0 and u.current()[1] == code                   # a runtime change: the usual update
    assert au.runtime_changes(["docs/a.md", "README.md", "tests/t.py", "sizeup/cloud/x.py"]) == ["sizeup/cloud/x.py"]
    assert au.runtime_changes(["deploy/station/windows/usb-attach.ps1", "data/notes.md"]) == \
        ["deploy/station/windows/usb-attach.ps1", "data/notes.md"]
    del docs


def test_only_a_full_sha_is_followed(station):
    repo, u = station
    new = repo.commit("two")
    u.want(new[:7])                                                  # a short name can match a branch first
    assert u.run() == 0 and u.calls == []
    assert "isn't a full git sha" in u.load()["paused"]


def test_a_branch_named_like_a_sha_never_wins(tmp_path):
    """install-linux.sh --stage resolves the 40-hex sha as an object, not as a ref of that name."""
    repo = Repo(tmp_path)
    on_main = repo.shas[0]
    side = repo.commit("planted", branch="feature")
    git(repo.origin, "branch", "-f", on_main, side)                  # a branch called <the main sha>
    bare = repo.opt / "repo.git"
    git(repo.opt, "--git-dir=repo.git", "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
    assert git(repo.opt, "--git-dir=repo.git", "rev-parse", f"refs/remotes/origin/{on_main}") == side
    got = git(repo.opt, "--git-dir=repo.git", "rev-parse", "-q", "--verify", f"{on_main}^{{commit}}")
    assert got == on_main and bare.is_dir()
    sh = (ROOT / "deploy" / "station" / "install-linux.sh").read_text()
    stage = sh[sh.index('if [ -n "$STAGE_SHA" ]; then\n  # --'):sh.index('if [ -n "$ACTIVATE_NAME" ]; then\n')]
    for want in ('[[ "$STAGE_SHA" =~ ^[0-9a-f]{40}$ ]]', 'rev-parse -q --verify "$STAGE_SHA^{commit}"',
                 '[ "$sha" = "$STAGE_SHA" ]', 'merge-base --is-ancestor "$sha" "refs/remotes/origin/main"'):
        assert want in stage, want
    assert "resolve_ref" not in stage


def test_want_file_never_follows_a_link_waits_on_a_fifo_or_reads_much(station):
    repo, u = station
    new = repo.commit("two")
    target = u.p.want.parent / "elsewhere"
    target.write_text(new + "\n")
    u.p.want.symlink_to(target)
    assert u.read_want() == ""
    u.p.want.unlink()
    os.mkfifo(u.p.want)
    t0 = time.time()
    assert u.read_want() == "" and time.time() - t0 < 1
    u.p.want.unlink()
    u.p.want.write_text(new + "\n" + "x" * 10_000_000)
    assert u.read_want() == new


def test_the_installer_runs_under_the_updaters_lock(tmp_path):
    script = tmp_path / "fake.sh"
    script.write_text('echo "locked=$SIZEUP_UPDATE_LOCKED"\nexit 87\n')
    lines = []
    u = Updater(Paths(opt=tmp_path), out=lines.append)
    with pytest.raises(StepError) as e:
        u.installer(["--stage", A], script)
    assert lines == ["locked=1"] and e.value.code == au.PREFLIGHT_EXIT
    sh = (ROOT / "deploy" / "station" / "install-linux.sh").read_text()
    assert "exit 87" in sh and "exit 3" not in sh and au.PREFLIGHT_EXIT == 87
    assert 'flock -n 9' in sh and '"${SIZEUP_UPDATE_LOCKED:-}"' in sh and "/run/sizeup-update/lock" in sh
    assert "systemctl stop sizeup-update.path sizeup-update.timer" in sh


def test_the_port_default_is_sizeups(tmp_path):
    from sizeup.config import load
    conf = tmp_path / "config.toml"
    conf.write_text('[station]\nid = "t"\n')
    u = Updater(Paths(config=conf, install_conf=tmp_path / "none"))
    assert u.settings()["port"] == load(conf).port == au.PORT


def test_status_and_doctor_print(station, capsys):
    repo, u = station
    new = repo.commit("two")
    u.want(new)
    u.run()
    r = u.report()
    assert r["state"] == "ok" and r["record"]["last"]["result"] == "ok"
    lines = au.status_lines(r)
    assert lines[0].startswith(f"version:   {new[:7]}") and any("last update" in ln and "ok" in ln for ln in lines)
    assert any("version" in ln and ln.startswith("  ok") for ln in au.doctor_lines(r))


def test_status_and_doctor_after_a_rollback_and_with_any_old_state(station, monkeypatch, capsys):
    repo, u = station
    old, new = repo.shas[0], repo.commit("two")
    u.want(new)
    u.run()
    assert u.rollback() == 0
    monkeypatch.setattr(au, "Updater", lambda **kw: u)
    for cmd in ("status", "doctor"):
        assert au.main([cmd]) == 0
    out = capsys.readouterr().out
    assert f"version:   {old[:7]}" in out and "rolled back by hand" in out
    assert "FAIL" in out and new[:7] in out                          # doctor: the one left behind failed
    for junk in ('"a string"', '{"last": "x", "failed": [1], "state": 3}', "not json"):
        u.p.state.write_text(junk)
        for cmd in ("status", "doctor"):
            assert au.main([cmd]) == 0                              # never a traceback on a station
    assert "Traceback" not in capsys.readouterr().out


def test_a_rollback_holds_while_the_hub_sits_on_a_docs_only_descendant(station):
    repo, u = station
    p = repo.shas[0]
    a = repo.commit("A: code", path="sizeup/a.py")
    u.want(a)
    u.run()
    c = repo.commit("C: words", path="docs/c.md")                    # the hub moves on, docs only
    u.want(c)
    u.run()
    assert u.current()[1] == a and u.load()["equivalent"][c] == a
    assert u.rollback() == 0 and u.current()[1] == p                 # A misbehaves: back to P
    u.calls.clear()
    for _ in range(3):
        u.t[0] += 900
        assert u.run() == 0
    assert u.calls == [] and u.current()[1] == p                     # C is A's code: not taken again
    assert describe(p, c, Watch(own="", run_dir="", state_path=str(u.p.state)).state())["state"] == "failed"
    d = repo.commit("D: a fix", path="sizeup/a.py")                  # the hub moves to real new code
    u.want(d)
    assert u.run() == 0 and u.current()[1] == d


# -- one switch per burst of hub deploys, never mid-page, with a ceiling -----------------------------------

def test_a_burst_of_hub_deploys_is_one_switch_to_the_newest(station):
    repo, u = station
    u.settle_s = au.SETTLE_S
    b1, b2, b3 = repo.commit("two"), repo.commit("three"), repo.commit("four")
    t0 = u.t[0]
    deploys = {t0 + 300: b2, t0 + 700: b3}                   # the hub deploys again 5 and 12 min later

    def deploy(now):
        for at in sorted(deploys):
            if now >= at:
                u.want(deploys.pop(at))
    u.on_sleep = deploy
    u.want(b1)
    assert u.run() == 0
    assert u.calls == [("stage", b1), ("stage", b2), ("stage", b3), ("activate", b3)]   # one restart
    assert u.current()[1] == b3
    # b3 seen at +700 (the first poll after it): switched once it held still 10 min
    assert t0 + 700 + au.SETTLE_S <= u.t[0] < t0 + 700 + au.SETTLE_S + au.SETTLE_POLL_S + 1
    st = u.load()
    assert st["state"] == "current" and "pending" not in st and "hold" not in st
    assert st["last"]["skipped"] == [b1[:7], b2[:7]] and st["last"]["went"] == "a quiet moment"
    line = st["last"]["line"]
    assert f"-> {b3[:7]}: ok in " in line and "2 hub build(s) in between skipped" in line and "settle " in line
    said = "\n".join(u.lines)
    assert f"the hub moved on to {b2[:7]} while this waited: {b1[:7]} skipped" in said
    assert "waiting (settle): the hub's build is 0 s old: switching once it has held still 10 min" in said
    assert f"in between skipped: {b1[:7]}, {b2[:7]}" in said


def test_it_waits_out_a_page_and_a_long_queue_and_says_why(station):
    repo, u = station
    u.settle_s = au.SETTLE_S
    new = repo.commit("two")
    t0 = u.t[0]
    u.busy_for = 30                                          # a page / carrier for 30 polls (2.5 min) once settled
    u.stt = 80                                               # whisper far behind

    def drain(now):
        if now - t0 >= au.SETTLE_S + 1500:
            u.stt = 12                                       # then it catches up a little
    u.on_sleep = drain
    u.want(new)
    assert u.run() == 0 and u.calls == [("stage", new), ("activate", new)]
    st = u.load()
    # settled at +600; carrier until ~+750; 80 waiting: more than 5, 20 (from +1200), 60 (from +1800); 12 at +2100
    assert t0 + au.SETTLE_S + 1500 <= u.t[0] < t0 + au.SETTLE_S + 1500 + 10
    assert st["last"]["stt_at_switch"] == 12 and st["last"]["went"] == "a quiet moment, 12 transcription(s) waiting"
    assert st["last"]["held"].startswith("settle 10 min, busy ") and ", stt " in st["last"]["held"]
    said = "\n".join(u.lines)
    assert "waiting (busy): carrier open on FG1" in said
    assert "waiting (stt): 80 transcription(s) waiting: switching at 5 or fewer" in said
    assert "at the switch: 12 transcription(s) in flight" in said and "at the switch: 12" in st["last"]["line"]
    # never a hold() poll while settling: the station isn't asked until the build has held still
    assert len(u.holds) < 400


def test_the_ceiling_switches_anyway_and_says_so(station):
    repo, u = station
    u.settle_s = au.SETTLE_S
    new = repo.commit("two")
    t0 = u.t[0]
    u.stt = 500
    u.busy_for = 10 ** 6                                     # someone on the air for ever
    u.want(new)
    assert u.run() == 0 and u.current()[1] == new
    assert t0 + au.CEILING_S + au.QUIET_MAX_S <= u.t[0] < t0 + au.CEILING_S + au.QUIET_MAX_S + 10
    st = u.load()
    assert st["last"]["went"] == "still carrier open on FG1 after 5 min: switching anyway"
    assert st["last"]["stt_at_switch"] == 500


def test_the_ceiling_counts_from_when_it_first_fell_behind(station):
    repo, u = station
    u.settle_s = au.SETTLE_S
    builds = [repo.commit(f"b{i}") for i in range(8)]
    t0 = u.t[0]
    deploys = {t0 + 400 * (i + 1): b for i, b in enumerate(builds[1:])}   # a deploy every 6.7 min for 53 min

    def deploy(now):
        for at in sorted(deploys):
            if now >= at:
                u.want(deploys.pop(at))
    u.on_sleep = deploy
    u.want(builds[0])
    assert u.run() == 0
    acts = [c for c in u.calls if c[0] == "activate"]
    assert len(acts) == 1                                    # never settled, but switched once at the ceiling
    assert t0 + au.CEILING_S <= u.t[0] < t0 + au.CEILING_S + 20
    assert "behind the hub (the most it waits: 45 min)" in u.load()["last"]["went"]


def test_an_urgent_build_or_autoupdate_now_goes_without_waiting(station):
    repo, u = station
    u.settle_s = au.SETTLE_S
    u.stt = 300
    git(repo.origin, "commit", "-q", "--allow-empty", "-m", "Fix the radio\n\nStation-Update: now")
    urgent = git(repo.origin, "rev-parse", "HEAD")
    new = repo.commit("after it")                            # merged on top: still urgent (since the running build)
    t0 = u.t[0]
    u.want(new)
    assert u.run() == 0 and u.current()[1] == new and u.t[0] - t0 < 5
    assert u.load()["last"]["went"] == "urgent, 300 transcription(s) waiting"
    assert f"urgent build (Station-Update: now in {urgent[:7]})" in "\n".join(u.lines)
    # sizeup-station autoupdate now: the mark goes after the switch
    later = repo.commit("five")
    u.p.work.mkdir(parents=True, exist_ok=True)
    u.p.now_mark.touch()                                     # (what the CLI writes: test_the_cli_marks_now)
    t1 = u.t[0]
    u.want(later)
    assert u.run() == 0 and u.current()[1] == later and u.t[0] - t1 < 5 and not u.p.now_mark.exists()
    # an ordinary build after that settles again
    plain = repo.commit("six")
    t2 = u.t[0]
    u.want(plain)
    assert u.run() == 0 and u.t[0] - t2 >= au.SETTLE_S


def test_the_cli_marks_now(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "Paths", lambda: Paths(work=tmp_path / "work"))
    assert au.main(["now"]) == 0 and (tmp_path / "work" / "now").is_file()


def test_a_deferred_switch_is_shown_and_counts_for_the_deploy_check(station):
    import importlib.util
    repo, u = station
    u.settle_s = au.SETTLE_S
    new = repo.commit("two")
    seen = []

    def look(now):
        if not seen:
            seen.append(u.load())
    u.on_sleep = look
    u.want(new)
    u.run()
    st = seen[0]
    assert st["state"] == "deferred" and st["hold"]["phase"] == "settle"
    assert st["hold"]["until"] == st["pending"]["behind"] + au.CEILING_S + au.QUIET_MAX_S
    old = repo.shas[0]
    w = Watch(own=old, run_dir="", state_path=str(u.p.state), log=lambda m: None)
    u.p.state.write_text(json.dumps(st))
    v = w.state()
    assert v["hold"]["phase"] == "settle"
    d = describe(old, new, v, now=st["since"] + 10)
    assert d["state"] == "deferred" and d["text"].startswith(f"updating {old[:7]} -> {new[:7]}: staged, switching by ")
    assert "held still 10 min" in d["text"]
    assert describe(old, new, v, now=st["hold"]["until"] + 600)["state"] == "behind"     # past it: something's wrong
    spec = importlib.util.spec_from_file_location("deploy_check", Path(__file__).parent.parent / "tools" / "deploy_check.py")
    dc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dc)
    ok, msg = dc.verdict({"hub_build": new, "station": {"reported": True, "state": d["state"], "text": d["text"]}}, new)
    assert ok == dc.OK and "staged, switching by" in msg


def test_station_stop_hands_over_the_clips_still_waiting(tmp_path):
    import numpy as np
    from sizeup.app import App, Rerun
    from sizeup.incidents import Traffic
    from sizeup.stt import CATCHUP, LIVE, LOW, RERUN, SHED, URGENT
    from tests.test_app import cfg
    app = App(cfg(tmp_path))
    audio = np.zeros(1600, np.float32)
    clips = [Traffic(f"P25-{i}", "Fire/EMS 15", 100.0 + i, 101.0 + i, "", status="pending") for i in range(3)]
    app.stt.submit(clips[0], audio, URGENT, "tg:1")
    app.stt.submit(clips[1], audio, LIVE, "tg:2")
    app.stt.submit(clips[2], audio, LOW, "tg:3")
    mine = Rerun("P25-9", "traffic", False)
    app._rerun_ids.add(mine.id)
    app.stt.submit(mine, audio, RERUN)
    caught = Rerun("P25-8", "traffic", False, catchup=True)
    app._rerun_ids.add(caught.id)
    app._catchup_ids.add(caught.id)
    app.stt.submit(caught, audio, CATCHUP)
    n = app._stt_handoff()
    assert n == {"waiting": 5, "skipped": 3, "pages": 0, "reruns": 1, "catchup": 1, "unmarked": 0, "in_flight": 0}
    assert app.stt.busy() == 0
    assert all(t.status == "skipped" and f"({SHED}: the station stopped" in t.model for t in clips)
    assert caught.id not in app._rerun_ids and mine.id not in app._rerun_ids
    # the rows are there, and the catch-up finds them (sizeup/sttcatchup.py: Comms.shed_clips)
    rows, _ = app.cad.comms.shed_clips(0)
    assert sorted(r["id"] for r in rows) == ["P25-0", "P25-1", "P25-2"]
    assert app._stt_handoff() == {"waiting": 0, "skipped": 0, "pages": 0, "reruns": 0, "catchup": 0, "unmarked": 0,
                                  "in_flight": 0}


# -- review fixes (#712) -------------------------------------------------------------------------------------

def _snapshots(u):
    seen = []
    u.on_sleep = lambda now: seen.append(u.load())
    return seen


def test_a_station_that_gets_here_late_still_never_switches_mid_page_at_once(station):
    repo, u = station
    u.settle_s = au.SETTLE_S
    old, new = repo.shas[0], repo.commit("two")
    t0 = u.t[0]
    # behind for an hour already (a long build, a stage retried, a build that failed before): late on arrival
    u.save({}, pending={"sha": new, "seen": t0 - 3600, "behind": t0 - 3600, "skipped": []})
    u.busy_for = 10 ** 6                                     # a page on the air the whole time
    seen = _snapshots(u)
    u.want(new)
    assert u.run() == 0 and u.calls == [("stage", new), ("activate", new)]
    assert t0 + au.QUIET_MAX_S <= u.t[0] < t0 + au.QUIET_MAX_S + 10            # 5 min of grace from now, not none
    assert u.load()["last"]["went"] == "still carrier open on FG1 after 5 min: switching anyway"
    mid = seen[len(seen) // 2]
    assert mid["state"] == "deferred" and mid["hold"]["phase"] == "busy"
    assert t0 + au.QUIET_MAX_S <= mid["hold"]["until"] < t0 + au.QUIET_MAX_S + 5
    d = describe(old, new, mid, now=mid["since"] + 200)
    assert d["state"] == "deferred" and "staged, switching by" in d["text"]


def test_a_build_that_failed_before_doesnt_carry_its_ceiling_over(station):
    repo, u = station
    bad = repo.commit("two")
    u.bad_preflight.add(bad)
    u.want(bad)
    assert u.run() == 1 and "pending" in u.load()
    assert u.run() == 0 and u.load()["state"] == "failed-before" and "pending" not in u.load()
    u.p.work.mkdir(parents=True, exist_ok=True)
    u.p.now_mark.touch()                                     # a "now" left over never makes a later update urgent
    assert u.run() == 0 and not u.p.now_mark.exists()


def test_autoupdate_off_stops_a_deferred_switch(station):
    repo, u = station
    u.settle_s = au.SETTLE_S
    new = repo.commit("two")
    t0 = u.t[0]

    def off(now):
        if now - t0 >= 120:
            u.p.install_conf.write_text("SIZEUP_REF=main\nSIZEUP_AUTO_UPDATE=off\n")
    u.on_sleep = off
    u.want(new)
    assert u.run() == 0 and u.calls == [("stage", new)]     # built, never switched
    assert u.t[0] - t0 < 140
    st = u.load()
    assert st["state"] == "paused" and "autoupdate on" in st["paused"] and "hold" not in st and "pending" not in st
    assert "not switching: auto-update is off" in "\n".join(u.lines)


def test_once_late_it_switches_to_the_staged_build_rather_than_chase_a_newer_one(station):
    repo, u = station
    u.settle_s = au.SETTLE_S
    new, newer = repo.commit("two"), repo.commit("three")
    t0 = u.t[0]
    u.save({}, pending={"sha": new, "seen": t0 - au.CEILING_S - 10, "behind": t0 - au.CEILING_S - 10, "skipped": []})
    u.busy_for = 4                                           # a carrier for 20 s

    def deploy(now):
        if now - t0 >= 5 and not u.p.want.read_text().startswith(newer):
            u.want(newer)
    u.on_sleep = deploy
    u.want(new)
    assert u.run() == 0
    assert u.calls[:2] == [("stage", new), ("activate", new)]                 # the staged one, at once
    assert u.calls[2:] == [("stage", newer), ("activate", newer)]             # then the newer one, settled
    assert u.current()[1] == newer


def test_a_page_waiting_at_stop_gets_its_seconds_then_is_marked_like_a_shed_clip(tmp_path):
    import numpy as np
    from sizeup.app import App
    from sizeup.radio.page import Page
    from sizeup.stt import SHED, URGENT
    from tests.test_app import cfg
    app = App(cfg(tmp_path))
    app.whisper.state = "ok"                                 # whisper up: the URGENT ones get their seconds
    page = Page("South2-1", "South 2", 46.0, 100.0, 110.0, "voice", audio=np.zeros(16000, np.float32))
    app.stt.submit(page, np.zeros(16000, np.float32), URGENT, "South 2")
    t0 = time.monotonic()
    n = app._stt_handoff(wait_s=0.5)
    assert 0.5 <= time.monotonic() - t0 < 5
    assert n["pages"] == 1 and n["waiting"] == 1 and app.stt.busy() == 0
    assert page.status == "skipped" and f"({SHED}: the station stopped" in page.model


def _late(u, sha):
    t0 = u.t[0]
    u.save({}, pending={"sha": sha, "seen": t0 - au.CEILING_S - 10, "behind": t0 - au.CEILING_S - 10, "skipped": []})
    u.busy_for = 4                                           # a carrier for 20 s: the want is read meanwhile
    return t0


def test_once_late_the_hub_going_back_or_elsewhere_still_stops_the_switch(station):
    repo, u = station
    u.settle_s = au.SETTLE_S
    old, new = repo.shas[0], repo.commit("two")
    t0 = _late(u, new)
    u.on_sleep = lambda now: u.want(old) if now - t0 >= 5 else None      # the hub rolled back to what runs here
    u.want(new)
    assert u.run() == 0 and ("activate", new) not in u.calls and u.current()[1] == old
    assert f"the hub went back to {old[:7]}, the build this station runs: not switching" in "\n".join(u.lines)
    # ... or to a build that isn't after the staged one (a side branch the hub runs)
    u.calls.clear()
    side = repo.commit("elsewhere", branch="other")
    git(repo.origin, "checkout", "-q", "main")
    git(repo.origin, "merge", "-q", "--no-ff", "-m", "merge other", "other")
    elsewhere = git(repo.origin, "rev-parse", "other")                    # on main (merged), not after `new`
    newest = repo.commit("three")
    t1 = _late(u, newest)
    u.on_sleep = lambda now: u.want(elsewhere) if now - t1 >= 5 else None
    u.want(newest)
    u.run()
    assert u.calls[0] == ("stage", newest) and ("activate", newest) not in u.calls
    assert f"the hub moved to {elsewhere[:7]}, not a build after {newest[:7]}: not switching to it" in "\n".join(u.lines)
    assert side == elsewhere


def test_autoupdate_off_at_the_last_moment_switches_nothing(station, monkeypatch):
    repo, u = station
    new = repo.commit("two")
    real = u.pick_moment

    def then_off(*a, **k):
        got = real(*a, **k)
        u.p.install_conf.write_text("SIZEUP_REF=main\nSIZEUP_AUTO_UPDATE=off\n")
        return got
    monkeypatch.setattr(u, "pick_moment", then_off)
    u.want(new)
    assert u.run() == 0 and u.calls == [("stage", new)] and u.load()["state"] == "paused"


def test_the_off_command_only_says_it_stopped_the_switch_when_it_did():
    text = (Path(__file__).parent.parent / "deploy" / "station" / "sizeup-station").read_text()
    off = text[text.index("      on|off)"):text.index("      now)")]
    assert 'if [ -n "$stopped" ] || [ "$(updater_state)" = paused ]; then' in off
    assert off.index('systemctl stop sizeup-update.service && stopped=1') < off.index("the waiting update won't switch")


def test_a_fetch_that_hangs_while_late_still_switches_to_the_staged_build(station, monkeypatch):
    repo, u = station
    u.settle_s = au.SETTLE_S
    new = repo.commit("two")
    t0 = _late(u, new)
    u.busy_for = 10 ** 6                                     # a carrier on: the 5 min grace, then anyway
    unseen = []                                              # the hub's next build: made after the station fetched
    real = au.subprocess.run
    tried = []

    def run(argv, *a, **k):
        if "fetch" in argv and tried is not None and u.calls:          # the network goes once it has staged
            tried.append(k.get("timeout"))
            raise subprocess.TimeoutExpired(argv, k.get("timeout") or 0)
        return real(argv, *a, **k)
    monkeypatch.setattr(au.subprocess, "run", run)

    def deploy(now):
        if now - t0 >= 5 and not unseen:
            unseen.append(repo.commit("three"))
            u.want(unseen[0])
    u.on_sleep = deploy
    u.want(new)
    assert u.run() == 1                                      # the switch, then the newer one can't be fetched
    unseen = unseen[0]
    assert u.calls[:2] == [("stage", new), ("activate", new)] and u.current()[1] == new
    log = u.p.log.read_text().splitlines()
    assert f"-> {new[:7]}: ok in " in log[0] and "went: still carrier open on FG1 after 5 min: switching anyway" in log[0]
    said = "\n".join(u.lines)
    assert f"can't look up the hub's {unseen[:7]} (git fetch: timed out after 60 s): switching to the staged build" in said
    assert tried[0] == 60
    st = u.load()
    assert st["state"] == "behind" and st["last"]["result"] == "no fetch" and "timed out after 600 s" in st["last"]["why"]
