"""The station's private incident-source adapter (GitHub #301; the interface: #297), installed and run on the station
only: `sizeup-station adapter setup` (a fake key, fake prompts), the updater fetching it from a local bare git repo
standing in for the private one and installing it into a copy of the release (a real venv, a fake pip, the real
self-check), a preflight failure, a rollback, removal, the hub refusing, and no setup meaning nothing changes."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
import venv
import zipfile
from pathlib import Path

import pytest

from sizeup.cloud import autoupdate as au
from sizeup.cloud import configupdate as cu
from sizeup.cloud import adapters
from sizeup.cloud.adapters import HubRefused
from sizeup.cloud.autoupdate import StepError
from tests.test_autoupdate import FakeUpdater, Repo, git

ROOT = Path(__file__).resolve().parent.parent
SITE = f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFake sizeup-adapter@test"


# -- a stand-in for the private repo: a package with a sizeup.incident_sources entry point --------------------------

class AdapterRepo:
    def __init__(self, tmp: Path):
        self.work = tmp / "adapter-work"
        self.work.mkdir()
        self.bare = tmp / "adapter.git"
        git(self.work, "init", "-q", "-b", "main")
        (self.work / "pyproject.toml").write_text(
            '[project]\nname = "feed-adapter"\nversion = "0.1"\n\n'
            '[project.entry-points."sizeup.incident_sources"]\nfeed = "feed_adapter:make"\n')
        self.commits = [self.commit("def make(ctx):\n    return []\n\ndef self_check():\n    return ''\n")]
        subprocess.run(["git", "clone", "-q", "--bare", str(self.work), str(self.bare)], check=True)

    def commit(self, code: str) -> str:
        (self.work / "feed_adapter").mkdir(exist_ok=True)
        (self.work / "feed_adapter" / "__init__.py").write_text(code)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "adapter")
        sha = git(self.work, "rev-parse", "HEAD")
        if self.bare.exists():
            git(self.work, "push", "-q", str(self.bare), "main")
        return sha


class AdapterUpdater(FakeUpdater):
    """FakeUpdater (the installer, /healthz and the radios faked) with real releases' venvs, the adapter really
    fetched and archived, a fake pip (a copy and a dist-info, no network) and the real self-check."""

    def __init__(self, repo: Repo, tmp: Path, **kw):
        super().__init__(repo, tmp, **kw)
        self.p.adapter_key = tmp / "etc" / "adapter-deploy-key"
        self.p.adapter_pending = tmp / "etc" / "adapter-pending.json"
        self.p.env = tmp / "etc" / "station.env"
        self.p.marks = tmp / "etc" / "config-updates"
        self.p.config = tmp / "etc" / "config.toml"
        self.p.known_hosts = tmp / "etc" / "known_hosts"
        self.p.adapter_key.parent.mkdir(exist_ok=True)
        self.p.config.write_text('[station]\nid = "test"\n')
        self.p.credentials = tmp / "etc" / "credentials"
        self.p.dropin = tmp / "systemd" / "sizeup-station.service.d" / "adapter-credentials.conf"
        self.pips: list[tuple] = []
        self.builds: list[str] = []
        self.wheel_fail = self.pip_fail = False
        self.extra_files: dict[str, str] = {}
        self.extra_record: list[str] = []
        self.systemctls: list[tuple] = []
        self.systemctl_ok, self.systemd = True, 255
        self.unhealthy_releases: set[str] = set()

    def systemctl(self, *args):
        self.systemctls.append(args)
        return self.systemctl_ok

    def systemd_version(self):
        return self.systemd

    def healthy(self, sha):
        if self.current()[0] in self.unhealthy_releases:
            return "/healthz didn't answer within 90 s of the switch"
        return super().healthy(sha)

    def make_release(self, name, build, builds=""):
        super().make_release(name, build, builds)
        v = self.releases_dir / name / "venv"
        if not v.exists():
            venv.create(v, with_pip=False, symlinks=True)

    def build_wheels(self, rel, src, out):
        """pip wheel, faked: the package and its dist-info zipped as a wheel (no build backend, no network)."""
        self.builds.append(src.name)
        if self.wheel_fail:
            raise StepError("adapter: wheel build failed (exit 1)")
        meta = tomllib.loads((src / "pyproject.toml").read_text())["project"]
        dist, ver = meta["name"].replace("-", "_"), meta["version"]
        eps = meta.get("entry-points", {}).get(au.ADAPTER_GROUP, {})
        with zipfile.ZipFile(out / f"{dist}-{ver}-py3-none-any.whl", "w") as z:
            for f in sorted((src / "feed_adapter").rglob("*")):
                z.write(f, f"feed_adapter/{f.relative_to(src / 'feed_adapter')}")
            for extra, data in self.extra_files.items():
                z.writestr(extra, data)
            di = f"{dist}-{ver}.dist-info"
            z.writestr(f"{di}/METADATA", f"Metadata-Version: 2.1\nName: {meta['name']}\nVersion: {ver}\n")
            z.writestr(f"{di}/entry_points.txt",
                       f"[{au.ADAPTER_GROUP}]\n" + "".join(f"{k} = {v}\n" for k, v in eps.items()))
            z.writestr(f"{di}/RECORD", "".join(f"{n},,\n" for n in [*z.namelist(), f"{di}/RECORD", *self.extra_record]))

    def pip_install(self, rel, wheels, dist, drop):
        """The real install (no interpreter: unpacked by this process), recorded; pip_fail breaks it."""
        self.pips.append(("install", rel.name, dist, tuple(drop)))
        if self.pip_fail:
            raise StepError("adapter: pip install: ERROR: something")
        return super().pip_install(rel, wheels, dist, drop)

    def pip_uninstall(self, rel, dists):
        if dists:
            self.pips.append(("uninstall", rel.name, tuple(dists)))
        return super().pip_uninstall(rel, dists)

    def set_up(self, repo: Path, ref: str = "main") -> None:
        self.p.adapter_key.write_text("fake private key\n")
        self.p.adapter_key.chmod(0o600)
        self.p.adapter_pending.write_text(json.dumps({"repo": str(repo), "ref": ref, "env": ["FEED_USER"]}))
        if not self.cred_has("FEED_USER"):
            self.write_creds({"FEED_USER": "feed-user"})
        self._settings = None

    def installed(self) -> dict | None:
        return self.release_adapter(self.current()[0])


@pytest.fixture(autouse=True)
def allow_local(monkeypatch):
    """The stand-in for the private repo is a local path: allowed in tests and CI only."""
    monkeypatch.setenv(au.ALLOW_LOCAL, "1")


@pytest.fixture
def station(tmp_path):
    repo = Repo(tmp_path)
    u = AdapterUpdater(repo, tmp_path)
    u.make_release(repo.shas[0], repo.shas[0])
    u.switch(repo.shas[0])
    return repo, u, AdapterRepo(tmp_path)


# -- without a key or config: nothing changes -------------------------------------------------------------------

def test_no_adapter_set_up_changes_nothing(station):
    repo, u, _ad = station
    s = repo.shas[0]
    u.want(s)
    assert u.run() == 0 and u.calls == []                          # on the hub's build: nothing, as before
    assert not u.p.adapter_repo.exists() and "adapter" not in u.load()
    new = repo.commit("two")
    u.want(new)
    assert u.run() == 0
    assert u.calls == [("stage", new), ("activate", new)]          # the same update as ever: releases/<sha>
    assert u.current() == (new, new) and u.installed() is None and u.pips == []
    assert u.adapter() == {} and u.report()["adapter"]["state"] == "off"
    assert not any("adapter" in ln for ln in au.status_lines(u.report()) + au.doctor_lines(u.report()))


# -- the update: fetch, install, preflight, switch; a new commit alone rebuilds; a failure stays put ------------------

def test_the_adapter_is_fetched_installed_and_switched_to_side_by_side(station):
    repo, u, ad = station
    s, c1 = repo.shas[0], ad.commits[0]
    u.set_up(ad.bare)
    u.want(s)
    assert u.run() == 0
    name = au.release_name(s, c1)
    assert name == f"{s}-a{c1[:12]}"
    assert u.calls == [("stage", s), ("activate", name)]           # the same sha, a copy with the adapter, one switch
    assert u.current() == (name, s)
    got = u.installed()
    assert got["commit"] == c1 and got["dist"] == "feed-adapter" and got["sources"] == ["feed"]
    assert got["repo"] == str(ad.bare) and got["ref"] == "main"
    assert u.pips == [("install", f".staging-{name}", "feed-adapter", ())]
    # the release it was copied from is untouched: hard links, the adapter only in the copy's venv
    assert not (u.p.releases / s / "venv" / SITE / "feed_adapter").exists()
    assert (u.p.releases / name / "venv" / SITE / "feed_adapter" / "__init__.py").is_file()
    rec = u.load()["adapter"]
    assert rec["remote"] == c1 and rec["repo"] == "adapter" and rec["dists"] == ["feed-adapter"]
    assert u.load()["last"]["result"] == "ok" and u.load()["last"]["adapter"] == c1[:12]
    view = u.report()["adapter"]
    assert view["state"] == "installed" and view["text"] == f"adapter {c1[:7]} installed"
    assert any(ln.startswith("adapter:   adapter ") for ln in au.status_lines(u.report()))
    u.calls.clear()
    assert u.run() == 0 and u.calls == []                          # the same commit: nothing

    c2 = ad.commit("def make(ctx):\n    return []\n")                # a new adapter commit, sizeup unchanged
    ad.commits.append(c2)
    assert u.run() == 0
    assert u.current() == (au.release_name(s, c2), s) and u.installed()["commit"] == c2
    assert u.previous() == name                                     # a rollback goes back to the c1 one


def test_an_adapter_that_fails_its_preflight_is_never_switched_to(station):
    repo, u, ad = station
    s = repo.shas[0]
    u.set_up(ad.bare)
    u.want(s)
    u.run()
    good = u.current()[0]
    bad = ad.commit("def make(ctx):\n    return []\n\ndef self_check():\n    return 'FEED_USER is not set'\n")
    u.calls.clear()
    assert u.run() == 1
    assert u.current()[0] == good and ("activate", au.release_name(s, bad)) not in u.calls
    rec = u.load()["adapter"]
    assert "FEED_USER is not set" in rec["failed"][bad]["why"]
    assert u.load()["last"]["result"] == "adapter not built" and not u.load().get("failed")
    assert "not switched: the adapter didn't build" in u.p.log.read_text().splitlines()[-1]
    assert not (u.p.releases / au.release_name(s, bad)).exists()   # nothing left half-built
    view = u.report()["adapter"]
    assert view["state"] == "failing" and f"{bad[:7]} failed" in view["text"]
    assert any(ln.startswith("  FAIL  adapter") for ln in au.doctor_lines(u.report()))
    u.calls.clear()
    assert u.run() == 0 and u.calls == []                          # not tried again until the ref moves
    # an import error is a preflight failure too
    broken = ad.commit("import does_not_exist_anywhere\n")
    assert u.run() == 1 and u.current()[0] == good
    assert "ModuleNotFoundError" in u.load()["adapter"]["failed"][broken]["why"]
    # sizeup moves on meanwhile: the new release keeps the adapter that runs (not the broken one)
    new = repo.commit("two")
    u.want(new)
    assert u.run() == 0
    assert u.current()[1] == new and u.installed()["commit"] == ad.commits[0]


def test_an_adapter_with_no_entry_point_or_on_a_hub_fails_the_self_check(station, tmp_path):
    repo, u, ad = station
    rel = u.p.releases / repo.shas[0]
    with pytest.raises(StepError) as e:
        u.self_check(rel, "feed-adapter")                           # not installed in that venv
    assert e.value.code == au.PREFLIGHT_EXIT and "isn't installed" in str(e.value)
    r = subprocess.run([sys.executable, "-c", au.ADAPTER_CHECK, "feed-adapter", "hub", au.ADAPTER_GROUP],
                       capture_output=True, text=True)
    assert r.returncode == 3 and au.HUB_REFUSED in r.stdout       # the self-check itself refuses on a hub


def test_rollback_restores_the_previous_adapter_and_holds(station):
    repo, u, ad = station
    s, c1 = repo.shas[0], ad.commits[0]
    u.set_up(ad.bare)
    u.want(s)
    u.run()
    c2 = ad.commit("def make(ctx):\n    return []\n")
    u.run()
    assert u.installed()["commit"] == c2
    assert u.rollback() == 0
    assert u.current() == (au.release_name(s, c1), s) and u.installed()["commit"] == c1
    assert c2 in u.load()["adapter"]["failed"] and not u.load().get("failed")   # the adapter, not sizeup's sha
    u.calls.clear()
    assert u.run() == 0 and u.calls == []                          # not straight back to c2
    c3 = ad.commit("def make(ctx):\n    return []\n\n# fixed\n")    # the ref moves on: tried
    assert u.run() == 0 and u.installed()["commit"] == c3


def test_an_adapter_release_that_doesnt_come_up_is_switched_back(station):
    repo, u, ad = station
    s, c1 = repo.shas[0], ad.commits[0]
    u.set_up(ad.bare)
    u.want(s)
    u.run()
    c2 = ad.commit("def make(ctx):\n    return []\n")
    u.unhealthy.add(s)                     # /healthz never answers after the switch to the c2 release
    u.calls.clear()
    assert u.run() == 1
    assert u.calls == [("stage", s), ("activate", au.release_name(s, c2)), ("activate", au.release_name(s, c1))]
    assert u.installed()["commit"] == c1 and c2 in u.load()["adapter"]["failed"]
    assert "not trying that adapter commit" in u.p.log.read_text().splitlines()[-1]


def test_a_fetch_that_fails_changes_nothing(station):
    repo, u, ad = station
    u.set_up(ad.bare)
    u.want(repo.shas[0])
    u.run()
    good = u.current()[0]
    u.set_up(ad.bare.with_name("gone.git"))
    u.calls.clear()
    assert u.run() == 0 and u.calls == [] and u.current()[0] == good
    assert "adapter git fetch" in u.load()["adapter"]["error"]
    assert "adapter git fetch" in u.report()["adapter"]["text"]


def test_removed_the_next_release_has_no_adapter(station, capsys):
    repo, u, ad = station
    s = repo.shas[0]
    u.set_up(ad.bare)
    u.want(s)
    u.run()
    u.p.env.write_text('SIZEUP_STATION_TOKEN=x\nFEED_USER="u"\n')      # (one kept there before LoadCredential=)
    u.write_creds({"FEED_USER": "u"})
    assert u.p.dropin.is_file()
    _current_release_with_configupdate(u)
    lines = []
    assert au.adapter_remove(u, yes=True, out=lines.append) == 0
    assert not u.p.adapter_key.exists() and not u.p.adapter_pending.exists() and not u.p.adapter_repo.exists()
    assert not u.cred_names() and not u.p.dropin.exists() and not u.p.adapter_wheels.exists()
    assert u.p.env.read_text() == "SIZEUP_STATION_TOKEN=x\n"          # its credentials out, the station's kept
    u._settings = None
    assert u.adapter() == {}
    u.calls.clear()
    assert u.run() == 0
    assert u.current() == (s, s) and u.installed() is None
    # releases/<s> (its venv linked from one that had the adapter) without it, before the switch to it
    assert u.pips[-1] == ("uninstall", s, ("feed-adapter",))


# -- setup: a fake key, fake prompts; the credentials hidden, in station.env only ----------------------------------

def _current_release_with_configupdate(u, prev_reads: bool | None = None) -> None:
    """The running release has this tree's configupdate.py and config.py; a previous one (a rollback's target) can
    read [adapter] or not."""
    cur = u.p.current.resolve()
    for f in ("sizeup/cloud/configupdate.py", "sizeup/config.py"):
        (cur / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / f, cur / f)
    if prev_reads is not None:
        prev = u.p.releases / ("p" * 40)
        u.make_release(prev.name, prev.name)
        (prev / "sizeup").mkdir()
        (prev / "sizeup" / "config.py").write_text("ADAPTER_KEYS = set()\n" if prev_reads else "# before #301\n")
        hist = u.p.releases / ".history"
        hist.write_text(prev.name + "\n" + hist.read_text())


def test_setup_makes_the_key_asks_hidden_and_records_the_repo(station):
    repo, u, ad = station
    _current_release_with_configupdate(u)
    u.p.env.write_text("# sizeup station secrets\nSIZEUP_STATION_TOKEN=tok-123\n")
    u.p.env.chmod(0o640)
    made = []

    def make_key(key):
        made.append(key)
        key.write_text("PRIVATE\n")
        key.with_name(key.name + ".pub").write_text(PUB + "\n")

    asked, hidden, said = [], [], []
    answers = iter(["git@github.com:example-org/feed-adapter.git", "", "FEED_USER, FEED_PASS", ""])   # ("": not from SSM)
    secrets = iter(['us"er$1', "p\\ass word`x"])
    rc = au.adapter_setup(u, ask=lambda p: (asked.append(p), next(answers))[1],
                          secret=lambda p: (hidden.append(p), next(secrets))[1], out=said.append, make_key=make_key,
                          check=False)
    assert rc == 0 and made == [u.p.adapter_key]
    assert stat.S_IMODE(u.p.adapter_key.stat().st_mode) == 0o600
    text = "\n".join(said)
    assert PUB in text and "PRIVATE" not in text
    assert f"echo '{PUB}' | gh repo deploy-key add - -R example-org/feed-adapter --title" in text
    assert hidden == ["FEED_USER (hidden): ", "FEED_PASS (hidden): "]       # getpass: never echoed
    for v in ('us"er$1', "p\\ass word`x"):
        assert v not in text                                                  # never printed
    # credentials: root-only files handed over by LoadCredential=, never in station.env (the environment)
    assert u.p.env.read_text() == "# sizeup station secrets\nSIZEUP_STATION_TOKEN=tok-123\n"
    assert u.cred_file("FEED_USER").read_text() == 'us"er$1'
    assert u.cred_file("FEED_USER").name == "sizeup-adapter.FEED_USER"           # (the credstore's naming)
    assert u.cred_file("FEED_PASS").read_text() == "p\\ass word`x"
    assert stat.S_IMODE(u.cred_file("FEED_PASS").stat().st_mode) == 0o600
    assert stat.S_IMODE(u.p.credentials.stat().st_mode) == 0o700
    assert "ImportCredential=sizeup-adapter.*" in u.p.dropin.read_text()       # systemd 255: a missing file is fine
    assert "LoadCredential" not in u.p.dropin.read_text()
    assert ("daemon-reload",) in u.systemctls and u.dropin_problem() == ""
    assert au.GITHUB_HOSTKEY in u.p.known_hosts.read_text()                  # pinned: StrictHostKeyChecking=yes
    assert au.GITHUB_HOSTKEY in (ROOT / "deploy" / "station" / "install-linux.sh").read_text()
    cfg = tomllib.loads(u.p.config.read_text())                               # no rollback target: written now
    assert cfg["adapter"] == {"repo": "git@github.com:example-org/feed-adapter.git", "ref": "main",
                              "env": ["FEED_USER", "FEED_PASS"]}
    assert not u.p.adapter_pending.exists() and "FEED" not in u.p.config.read_text().split("env =")[0]
    u._settings = None
    assert u.adapter() == {"repo": "git@github.com:example-org/feed-adapter.git", "ref": "main",
                           "env": ["FEED_USER", "FEED_PASS"], "why": ""}
    r = u.report()["adapter"]
    assert r["env"] == [("FEED_USER", True), ("FEED_PASS", True)] and r["state"] == "failing"   # set up, not in yet
    # again: Enter keeps each credential, a name dropped takes its value out
    said.clear()
    answers = iter(["", "v2", "FEED_USER", ""])
    assert au.adapter_setup(u, ask=lambda p: next(answers), secret=lambda p: "", out=said.append,
                            make_key=make_key, check=False) == 0
    assert len(made) == 1                                                     # the same key
    assert u.cred_file("FEED_USER").read_text() == 'us"er$1' and not u.cred_file("FEED_PASS").exists()
    assert tomllib.loads(u.p.config.read_text())["adapter"]["ref"] == "v2"


def test_setup_waits_with_the_table_while_a_rollback_couldnt_read_it(station):
    repo, u, ad = station
    _current_release_with_configupdate(u, prev_reads=False)
    u.p.adapter_key.write_text("PRIVATE\n")
    u.p.adapter_key.with_name(u.p.adapter_key.name + ".pub").write_text(PUB + "\n")
    said = []
    assert au.adapter_setup(u, repo=str(ad.bare), ref="main", env="", out=said.append, check=False) == 0
    assert "adapter" not in tomllib.loads(u.p.config.read_text())          # an older release would refuse it
    assert any("deferred" in ln for ln in said)
    assert json.loads(u.p.adapter_pending.read_text())["repo"] == str(ad.bare)
    u._settings = None
    assert u.adapter()["repo"] == str(ad.bare)                             # the updater has it meanwhile
    u.want(repo.shas[0])
    assert u.run() == 0 and u.installed()["commit"] == ad.commits[0]
    # the next switch leaves a release that reads it: written then
    leaving = u.p.releases / ("q" * 40)
    (leaving / "sizeup").mkdir(parents=True)
    (leaving / "sizeup" / "config.py").write_text("ADAPTER_KEYS = set()\n")
    assert cu.run(u.p.config, u.p.marks, leaving)[-1].startswith("adapter: [adapter] added")
    assert tomllib.loads(u.p.config.read_text())["adapter"]["repo"] == str(ad.bare)
    assert not u.p.adapter_pending.exists()


def test_setup_refuses_bad_input_and_the_hub(station):
    repo, u, ad = station
    out = []
    assert au.adapter_setup(u, repo="https://github.com/x/y", ref="main", env="", out=out.append, check=False) == 2
    assert au.adapter_setup(u, repo="git@github.com:x/y.git", ref="-x", env="", out=out.append, check=False) == 2
    assert au.adapter_setup(u, repo="git@github.com:x/y.git", ref="main", env="SIZEUP_STATION_TOKEN", out=out.append,
                            check=False) == 2
    assert not u.p.adapter_key.exists()
    u.p.config.write_text('[cloud]\nrole = "hub"\n')
    u._settings = None
    out.clear()
    assert au.adapter_setup(u, repo="git@github.com:x/y.git", ref="main", env="", out=out.append, check=False) == 2
    assert au.HUB_REFUSED in out[0] and not u.p.adapter_key.exists()


# -- the hub: a hard refusal -------------------------------------------------------------------------------------

def test_a_hub_refuses_to_load_or_run_adapters(station, tmp_path):
    repo, u, ad = station
    looked = []

    def eps(group):
        looked.append(group)
        return [type("EP", (), {"name": "feed"})()]

    with pytest.raises(HubRefused):
        adapters.discover("hub", entry_points=eps)
    assert looked == []                                    # refused before anything is looked at
    assert adapters.discover("station", entry_points=eps) == ["feed"]
    logs = []
    assert adapters.hub_guard("hub", logs.append, entry_points=eps) == ["feed"]
    assert "refused, never loaded" in logs[0]
    assert adapters.hub_guard("station", logs.append, entry_points=eps) == [] and len(logs) == 1
    # the updater on a machine whose config says hub: never fetches, never installs
    u.set_up(ad.bare)
    u.p.config.write_text('[cloud]\nrole = "hub"\n')
    u._settings = None
    assert u.adapter()["why"] == au.HUB_REFUSED
    u.want(repo.shas[0])
    assert u.run() == 0 and u.calls == [] and not u.p.adapter_repo.exists()
    assert u.pick_adapter({}, u.current()[0]) == (None, "", "")


def test_the_guard_is_the_one_297_defines():
    try:
        from sizeup.sources import runner
    except ImportError:
        pytest.skip("#297 (sizeup/sources) isn't on this branch yet: the fallback with the same names runs")
    assert adapters.require_station is runner.require_station and adapters.HubRefused is runner.HubRefused


def test_make_app_on_the_hub_runs_the_guard(monkeypatch):
    import sizeup.app as app_mod
    seen = []
    monkeypatch.setattr(adapters, "hub_guard", lambda role, log: seen.append(role) or [])
    import sizeup.cloud.hub as hub
    monkeypatch.setattr(hub, "HubApp", lambda cfg, log: "hub")
    cfg = type("C", (), {"cloud_role": "hub"})()
    assert app_mod.make_app(cfg) == "hub" and seen == ["hub"]


# -- what people see: names and states only -------------------------------------------------------------------------

def test_the_view_status_and_hub_health_show_no_credentials(station, tmp_path):
    from sizeup.cloud.health import station_adapter
    repo, u, ad = station
    u.set_up(ad.bare)
    u.want(repo.shas[0])
    u.run()
    w = au.Watch(own=repo.shas[0], run_dir="", state_path=str(u.p.state), clock=lambda: 1e9, log=lambda m: None,
                 release=u.p.current.resolve())
    v = w.status()["adapter"]
    assert v == {"state": "installed", "repo": "adapter", "commit": ad.commits[0][:12],
                 "text": f"adapter {ad.commits[0][:7]} installed"}
    assert str(ad.bare) not in json.dumps(w.status())                         # the URL stays on the station
    sent = dict(v, health=[{"source": "feed", "label": "Feed", "state": "ok", "reason": "", "password": "x"}],
                url="git@x:y/z.git", env={"FEED_PASS": "x"})
    h = station_adapter(sent)
    assert set(h) == {"state", "repo", "commit", "text", "health"} and "x" not in json.dumps(h["health"])
    assert station_adapter({"state": "off"}) is None and station_adapter(None) is None
    assert au.adapter_view(None, None)["state"] == "off"


def test_the_station_link_adds_each_sources_health_from_the_real_runner():
    """#304's real SourceRunner: a failing source reaches the station's report, the hub's Health model, and so the
    Reset button's data (health.js: build.adapter.health entries that are failing); an ok one doesn't."""
    from sizeup.cloud.health import StationHealth
    from sizeup.cloud.station import StationLink
    from sizeup.sources import Health
    from sizeup.sources.runner import SourceRunner
    from tests.test_health import FakeHub, cfg as hcfg, station_status

    class Feed:
        source, label, rank, poll_s = "feed", "Feed", 1, 30.0

        def poll(self):
            raise ConnectionError("https://user:pw@feed.example/ refused")    # (its text never goes anywhere)

        def health(self):
            return Health("ok")

        def reset(self):
            pass

    class Other(Feed):
        source, label, rank = "other", "Other", 2

        def poll(self):
            return []

    runner = SourceRunner([Feed(), Other()], lambda kind, data: None, log=lambda m: None)
    logs: list[str] = []
    fake = type("L", (), {})()
    fake.log = logs.append
    fake.updates = type("W", (), {"status": lambda self: {"adapter": {"state": "installed", "repo": "x",
                                                                       "text": "x 1234567 installed"}}})()
    fake.app = type("A", (), {"sources_runner": runner})()
    ad = StationLink.adapter(fake)                                            # not polled yet: no word either way
    assert [h["state"] for h in ad["health"]] == ["starting", "starting"] and ad["state"] == "installed"
    for r in runner.runs:
        runner.step(r)
    ad = StationLink.adapter(fake)
    assert ad["health"] == [{"source": "feed", "label": "Feed", "state": "failing", "reason": "ConnectionError"},
                            {"source": "other", "label": "Other", "state": "ok", "reason": ""}]
    assert ad["state"] == "failing" and "Feed failing (ConnectionError)" in ad["text"] and "pw@" not in str(ad)
    assert logs == []
    # the hub: Settings > Health's adapter row, and the Reset button's data (failing sources only)
    hub = FakeHub()
    hub.last_state = hub.now
    hub.status = station_status(version={"build": hub.build, "hub": hub.build, "update": {}, "adapter": ad})
    model = StationHealth(hub, hcfg(), None, None, lambda m: None, clock=lambda: hub.now).model()
    shown = model["build"]["adapter"]
    assert shown["state"] == "failing"
    assert [h["source"] for h in shown["health"] if h["state"] == "failing"] == ["feed"]
    # a runner this code can't read: said once, by type, and the status still goes
    fake.app = type("A", (), {"sources_runner": object()})()
    assert StationLink.adapter(fake)["health"] == [] and StationLink.adapter(fake)["health"] == []
    assert logs == ["hub link: incident sources' health not read: AttributeError"]
    fake.app = object()                                                       # no runner: no rows
    assert StationLink.adapter(fake)["health"] == []


# -- the config: [adapter], rollback-safe --------------------------------------------------------------------------

def test_the_config_reads_adapter_and_configupdate_adds_and_removes_it(tmp_path, monkeypatch):
    from sizeup.config import ConfigError, load
    base = (ROOT / "deploy" / "station" / "config.station.toml").read_text().replace(
        "/var/lib/sizeup/trunk-recorder/talkgroups.csv", f"{ROOT}/deploy/trunk-recorder/talkgroups.csv")
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path / "state"))
    want = {"repo": "git@github.com:o/r.git", "ref": "v1.2", "env": ["FEED_USER"]}
    new, said = cu.set_adapter(base, want)
    assert said == "adapter: [adapter] added (ref v1.2)" and new.startswith(base.rstrip("\n"))
    assert tomllib.loads(new)["adapter"] == want
    conf = tmp_path / "config.toml"
    conf.write_text(new)
    load(conf)                                                                # this release reads it
    again, said = cu.set_adapter(new, dict(want, ref="main"))
    assert tomllib.loads(again)["adapter"]["ref"] == "main" and again.count("[adapter]") == 1
    assert cu.set_adapter(again, dict(want, ref="main"))[1].endswith("nothing changed")
    gone, said = cu.remove_adapter(again)
    assert said == "adapter: [adapter] removed" and tomllib.loads(gone) == tomllib.loads(base)
    mid = "[station]\nid = \"x\"\n\n[adapter]\nrepo = \"/r\"\n\n# the web\n[web]\nport = 1\n"
    out, _ = cu.remove_adapter(mid)
    assert tomllib.loads(out) == {"station": {"id": "x"}, "web": {"port": 1}} and "# the web" in out
    with pytest.raises(ValueError):
        cu.set_adapter(base, dict(want, repo='x" = 1'))
    conf.write_text(base + '\n[adapter]\nenv = ["bad name"]\n')
    with pytest.raises(ConfigError):
        load(conf)
    assert cu.reads_adapter(ROOT) and cu.reads_adapter(None)
    assert not cu.reads_adapter(tmp_path / "no-such-release")


def test_helpers():
    assert au.adapter_repo_problem("git@github.com:o/r.git") == ""
    assert au.adapter_repo_problem("ssh://git@github.com/o/r.git") == ""
    assert au.adapter_repo_problem("/srv/adapter.git") == ""                  # (ALLOW_LOCAL: the fixture's)
    os.environ.pop(au.ALLOW_LOCAL)
    assert "only with SIZEUP_ADAPTER_ALLOW_LOCAL=1" in au.adapter_repo_problem("/srv/adapter.git")
    with pytest.raises(ValueError):
        cu.set_adapter("", {"repo": "/srv/adapter.git", "ref": "main", "env": []})
    for other_host in ("git@gitlab.com:o/r.git", "ssh://git@example.org/o/r.git", "git@github.com:-o/r"):
        assert au.adapter_repo_problem(other_host)
    for bad in ("https://github.com/o/r", "-oProxyCommand=x", "ext::sh -c x", "git@github.com:o/r;x", ""):
        assert au.adapter_repo_problem(bad)
    assert au.repo_name("git@github.com:o/feed.git") == "feed" and au.repo_name("/srv/x/adapter.git") == "adapter"
    assert au.github_slug("git@github.com:o/feed.git") == "o/feed"
    assert au.github_slug("ssh://git@github.com/o/feed") == "o/feed" and au.github_slug("/srv/a.git") == ""
    assert au.env_problem("FEED_USER") == "" and au.env_problem("PATH") and au.env_problem("LD_PRELOAD")
    assert au.env_problem("feed") and au.env_problem("SIZEUP_ACCESS_CLIENT_SECRET")


def test_the_installer_keeps_other_secrets_and_stages_the_adapter():
    sh = (ROOT / "deploy" / "station" / "install-linux.sh").read_text()
    body = sh[sh.index("ask_secrets() {"):sh.index("\n}\n", sh.index("ask_secrets() {"))]
    assert 'chmod 0600 "$envtmp"' in body and 'chown root:root "$envtmp"' in body
    assert "grep -vE '^(SIZEUP_STATION_TOKEN|SIZEUP_ACCESS_CLIENT_ID|SIZEUP_ACCESS_CLIENT_SECRET)=" in body
    assert 'autoupdate.py" adapter stage "$NAME"' in sh
    cli = (ROOT / "deploy" / "station" / "sizeup-station").read_text()
    assert 'python3 -I "$UPDATER" adapter "$@"' in cli and "adapter setup|status|remove" in cli


def test_cli_status_without_an_adapter(tmp_path, monkeypatch, capsys):
    paths = au.Paths
    monkeypatch.setattr(au, "Paths", lambda: paths(opt=tmp_path / "opt", config=tmp_path / "none.toml",
                                                      adapter_key=tmp_path / "none", state=tmp_path / "s.json",
                                                      adapter_pending=tmp_path / "none.json", want=tmp_path / "w",
                                                      install_conf=tmp_path / "i.conf"))
    assert au.main(["adapter", "status"]) == 0
    assert "none (sudo sizeup-station adapter setup)" in capsys.readouterr().out
    assert au.main(["adapter", "bogus"]) == 2
    with pytest.raises(SystemExit):
        au.main(["run", "extra"])


def test_credentials_root_only_files_and_station_env_kept(tmp_path):
    u = au.Updater(au.Paths(env=tmp_path / "station.env", credentials=tmp_path / "credstore",
                            dropin=tmp_path / "d" / "adapter-credentials.conf"))
    u.systemctl = lambda *a: True
    u.systemd_version = lambda: 252                       # Debian 12: LoadCredential= for each file there
    u.write_creds({"FEED_USER": "a b"})
    assert stat.S_IMODE(os.stat(u.cred_file("FEED_USER")).st_mode) == 0o600
    assert u.cred_has("FEED_USER") and not u.cred_has("FEED_PASS")
    assert u.p.dropin.read_text().count("LoadCredential=") == 1
    assert f"LoadCredential=sizeup-adapter.FEED_USER:{u.cred_file('FEED_USER')}" in u.p.dropin.read_text()
    u.cred_file("FEED_USER").unlink()                     # by hand: doctor says the drop-in doesn't match
    assert "doesn't match" in u.dropin_problem()
    with pytest.raises(ValueError):
        u.write_creds({"FEED_PASS": "two\nlines"})
    u.write_creds({}, drop={"FEED_USER"})
    assert u.cred_has("FEED_USER") is False and not u.p.dropin.exists() and u.dropin_problem() == ""
    (tmp_path / "station.env").write_text('# x\nSIZEUP_STATION_TOKEN=t\nFEED_USER="old"\n')
    u.drop_env({"FEED_USER"})
    assert (tmp_path / "station.env").read_text() == "# x\nSIZEUP_STATION_TOKEN=t\n"
    assert stat.S_IMODE(os.stat(tmp_path / "station.env").st_mode) == 0o600


def test_on_a_build_that_failed_the_adapter_still_follows_its_ref(station):
    repo, u, ad = station
    s = repo.shas[0]
    bad = repo.commit("two")
    u.unhealthy.add(bad)
    u.want(bad)
    assert u.run() == 1 and u.current()[0] == s                    # the hub's build failed: staying on s
    u.set_up(ad.bare)
    u.unhealthy.clear()
    u.calls.clear()
    assert u.run() == 0
    assert u.calls == [("stage", s), ("activate", au.release_name(s, ad.commits[0]))]
    assert u.load()["state"] == "current" and bad in u.load()["failed"]   # sizeup's failure stands


def test_paused_means_the_adapter_stays_put_too(station):
    repo, u, ad = station
    u.set_up(ad.bare)
    u.p.install_conf.write_text("SIZEUP_REF=main\nSIZEUP_AUTO_UPDATE=off\n")
    u._settings = None
    u.want(repo.shas[0])
    assert u.run() == 0 and u.calls == [] and not u.p.adapter_repo.exists()


def test_doctor_fails_a_key_others_can_read(station):
    repo, u, ad = station
    u.set_up(ad.bare)
    u.want(repo.shas[0])
    u.run()
    assert not any("want root 600" in ln for ln in au.doctor_lines(u.report()))
    u.p.adapter_key.chmod(0o644)
    assert any(ln.startswith("  FAIL  adapter: ") and "want root 600" in ln for ln in au.doctor_lines(u.report()))


def test_setup_says_so_while_an_update_holds_the_lock(station, monkeypatch):
    import fcntl
    from types import SimpleNamespace
    repo, u, ad = station
    monkeypatch.delenv("SIZEUP_UPDATE_LOCKED", raising=False)
    u.p.work.mkdir(parents=True, exist_ok=True)
    with (u.p.work / "lock").open("w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        a = SimpleNamespace(sub=["setup"], repo=str(ad.bare), ref="main", env="", no_check=True, yes=False)
        assert au.adapter_cli(u, a) == 1 and not u.p.adapter_key.exists()


# -- an adapter problem never fails a sizeup update (review of #305) -------------------------------------------------

def _with_adapter(station):
    repo, u, ad = station
    u.set_up(ad.bare)
    u.want(repo.shas[0])
    assert u.run() == 0
    return repo, u, ad, u.current()[0]


@pytest.mark.parametrize("broken", ["wheel", "pip", "archive"])
def test_a_new_adapter_commit_that_breaks_never_holds_up_a_sizeup_update(station, broken):
    """The commit at the ref breaks (its wheel build, pip, or git archive): the sizeup release is built with the
    adapter that runs (its cached wheels), and sizeup's sha is never recorded as failed."""
    repo, u, ad, good = _with_adapter(station)
    c1 = ad.commits[0]
    bad = ad.commit("def make(ctx):\n    return []\n\n# two\n")
    real_archive = u.archive

    def archive(commit, src):
        if commit == bad:
            raise StepError("adapter: git archive failed (exit 128)")
        return real_archive(commit, src)
    if broken == "archive":
        u.archive = archive
    elif broken == "wheel":
        u.wheel_fail = True                  # (c1's wheels are cached: not built again)
    new = repo.commit("two")
    u.want(new)
    if broken == "pip":
        real = u.pip_install
        u.pip_install = lambda rel, wheels, dist, drop: (_ for _ in ()).throw(StepError("adapter: pip install: no")) \
            if bad[:12] in rel.name else real(rel, wheels, dist, drop)
    assert u.run() == 0
    assert u.current() == (au.release_name(new, c1), new) and u.installed()["commit"] == c1
    assert not u.load().get("failed")
    rec = u.load()["adapter"]
    assert bad in rec["retry"] and "not built yet" in u.load()["last"]["adapter_why"]
    assert "adapter " in u.p.log.read_text().splitlines()[-1] and ": ok in " in u.p.log.read_text().splitlines()[-1]
    assert u.report()["adapter"]["state"] == "failing"                    # shown: a newer commit isn't in


def test_if_even_the_running_adapter_cant_be_installed_sizeup_goes_on_without_it(station):
    repo, u, ad, good = _with_adapter(station)
    u.pip_install = lambda *a: (_ for _ in ()).throw(StepError("adapter: wheel: build backend failed"))
    new = repo.commit("two")
    u.want(new)
    t = [1e9]
    u.clock = lambda: t[0]
    for _ in range(au.MAX_STAGES + 2):
        u.run()
        t[0] += 4000
    assert new not in u.load().get("failed", {})                         # (the reviewer's probe)
    assert u.current() == (new, new) and u.installed() is None           # without the adapter: paging is primary
    view = u.report()["adapter"]
    assert view["state"] == "failing" and "not installed" in view["text"]
    assert any(ln.startswith("  FAIL  adapter") for ln in au.doctor_lines(u.report()))
    assert ad.commits[0] in u.load()["adapter"]["failed"]                 # tried MAX_STAGES times, then failed


def test_an_adapter_only_build_that_breaks_backs_off_then_fails(station):
    repo, u, ad, good = _with_adapter(station)
    c2 = ad.commit("def make(ctx):\n    return []\n")
    n = []

    def boom(*a):
        n.append(1)
        raise StepError("adapter: wheel: build backend failed")
    u.pip_install = boom
    for _ in range(5):
        u.run()
    assert len(n) == 1                                                    # (the probe: not every run)
    assert u.load()["adapter"]["retry"][c2]["n"] == 1 and u.current()[0] == good
    for i in range(au.MAX_STAGES + 1):
        u.t[0] += 3601
        u.run()
    assert len(n) == au.MAX_STAGES and c2 in u.load()["adapter"]["failed"] and not u.load().get("failed")


def test_wheels_are_built_once_per_commit_and_reused(station):
    repo, u, ad, good = _with_adapter(station)
    assert len(u.builds) == 1
    cache = list(u.p.adapter_wheels.iterdir())
    assert len(cache) == 1 and json.loads((cache[0] / "wheels.json").read_text())["dist"] == "feed-adapter"
    u.wheel_fail = True
    for i in range(2):
        new = repo.commit(f"n{i}")
        u.want(new)
        assert u.run() == 0 and u.installed()["commit"] == ad.commits[0]
    assert len(u.builds) == 1                                             # no rebuild, no PyPI


@pytest.mark.parametrize("extra", [{"evil.pth": "import os"}, {"feed_adapter-0.1.data/scripts/x": "#!/bin/sh"}])
def test_root_refuses_a_wheel_with_a_pth_file_or_scripts(station, extra):
    repo, u, ad = station
    u.extra_files = extra
    u.set_up(ad.bare)
    u.want(repo.shas[0])
    assert u.run() == 1 and u.installed() is None and u.pips == []
    why = u.load()["adapter"]["failed"][ad.commits[0]]["why"]
    assert "wheel refused" in why and ("pth" in why or "scripts" in why)


def test_rollback_to_an_adapter_release_needs_the_adapter_set_up(station, capsys):
    repo, u, ad, withad = _with_adapter(station)
    s = repo.shas[0]
    u.p.adapter_key.unlink()
    u.p.adapter_pending.unlink()
    u._settings = None
    u.run()
    assert u.current()[0] == s
    assert u.rollback() == 1                                              # (the probe: it was undone at the next run)
    assert "Set it up first" in capsys.readouterr().err
    assert u.current()[0] == s and not u.load().get("failed")             # the running sha is never marked failed


def test_status_as_a_user_who_cant_read_etc_never_raises(station, monkeypatch):
    repo, u, ad, good = _with_adapter(station)
    import os.path as osp
    real = osp.isfile
    monkeypatch.setattr(au.os.path, "isfile", lambda p: False if "etc" in str(p) else real(p))

    def denied(self, *a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(au.Path, "stat", denied)
    r = u.report()                                                         # no exception
    assert r["adapter"]["key"] is False
    assert au.status_lines(r)


def test_a_child_gets_no_adapter_credentials(tmp_path, monkeypatch):
    from sizeup import procenv
    cred = tmp_path / "creds"
    cred.mkdir()
    (cred / "FEED_PASS").write_text("s3cret\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred))
    monkeypatch.setenv("FEED_PASS", "leaked-old-style")
    monkeypatch.setattr(procenv, "_dir", None)
    monkeypatch.setattr(procenv, "_names", set())
    assert procenv.take_credentials(["FEED_PASS"]) == cred
    assert "CREDENTIALS_DIRECTORY" not in os.environ and "FEED_PASS" not in os.environ
    assert adapters.credential("FEED_PASS") == "s3cret"
    assert adapters.credential("../FEED_PASS") == ""
    env = procenv.child_env(dict(os.environ, CREDENTIALS_DIRECTORY="/x", FEED_PASS="y"))
    out = subprocess.run([sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"], env=env,
                         capture_output=True, text=True, check=True).stdout
    child = json.loads(out)
    assert "FEED_PASS" not in child and "CREDENTIALS_DIRECTORY" not in child and "s3cret" not in out


def test_the_programs_sizeup_starts_get_an_explicit_env():
    for f, needle in (("sizeup/radio/fm.py", "env=child_env()"), ("sizeup/app.py", "env=child_env()"),
                      ("sizeup/tts.py", "env=child_env()"), ("sizeup/radio/recorder.py", "env = child_env()")):
        assert needle in (ROOT / f).read_text(), f


# -- re-review of #305 (b1516e3) ---------------------------------------------------------------------------------

def _install_other_dist(rel: Path) -> None:
    """A distribution already in the venv: 'otherpkg', claimed by its RECORD."""
    site = rel / "venv" / SITE
    (site / "otherpkg").mkdir(parents=True, exist_ok=True)
    (site / "otherpkg" / "__init__.py").write_text("")
    di = site / "otherpkg-1.0.dist-info"
    di.mkdir(exist_ok=True)
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: otherpkg\nVersion: 1.0\n")
    (di / "RECORD").write_text("otherpkg/__init__.py,,\notherpkg-1.0.dist-info/RECORD,,\nclaimed_only/x.py,,\n")


@pytest.mark.parametrize("extra, why", [
    ({"sitecustomize.py": "import os; os.system('id')"}, "sitecustomize"),
    ({"usercustomize.py": "x = 1"}, "usercustomize"),
    ({"sitecustomize/__init__.py": "x = 1"}, "sitecustomize"),        # a package of that name too
    ({"feed_adapter-0.1.data/data/bin/python": "#!/bin/sh"}, ".data"),
    ({"otherpkg/evil.py": "x = 1"}, "would replace otherpkg"),         # into another package's directory
    ({"claimed_only.py": "x = 1"}, "would replace claimed_only"),      # a name another dist's RECORD claims
    ({"json.py": "x = 1"}, ""),                                         # (a stdlib name: not site-packages', allowed)
])
def test_root_refuses_wheels_that_could_run_or_shadow_code(station, extra, why):
    repo, u, ad = station
    _install_other_dist(u.p.releases / repo.shas[0])
    u.extra_files = extra
    u.set_up(ad.bare)
    u.want(repo.shas[0])
    rc = u.run()
    if not why:
        assert rc == 0 and u.installed() is not None
        return
    assert rc == 1 and u.installed() is None
    got = u.load()["adapter"]["failed"][ad.commits[0]]["why"]
    assert "wheel refused" in got and why in got
    assert not (u.p.releases / repo.shas[0] / "venv" / SITE / "sitecustomize.py").exists()


def test_no_root_run_interpreter_reads_the_adapter_release(station, monkeypatch):
    """Installing and uninstalling the adapter starts no process at all (no pip, no venv python): only the sizeup
    user's wheel build and self-check run anything."""
    repo, u, ad = station
    started = []
    real_run, real_popen = subprocess.run, subprocess.Popen
    in_install = [False]

    def watch(fn):
        def w(argv, *a, **k):
            if in_install[0]:
                started.append(list(argv))
            return fn(argv, *a, **k)
        return w
    monkeypatch.setattr(au.subprocess, "run", watch(real_run))
    monkeypatch.setattr(au.subprocess, "Popen", watch(real_popen))
    base_install, base_uninstall = au.Updater.pip_install, au.Updater.pip_uninstall

    def install(self, *a):
        in_install[0] = True
        try:
            return base_install(self, *a)
        finally:
            in_install[0] = False
    monkeypatch.setattr(au.Updater, "pip_install", install)
    u.set_up(ad.bare)
    u.want(repo.shas[0])
    assert u.run() == 0 and u.installed() is not None
    c2 = ad.commit("def make(ctx):\n    return []\n")
    assert u.run() == 0 and u.installed()["commit"] == c2                 # the old version out by its RECORD, too
    assert started == []
    site = u.p.current.resolve() / "venv" / SITE
    assert [d.name for d in site.glob("feed_adapter-*.dist-info")] == ["feed_adapter-0.1.dist-info"]
    assert (site / "feed_adapter-0.1.dist-info" / "INSTALLER").read_text() == "sizeup\n"
    for f in ("deploy/station/install-linux.sh",):
        sh = (ROOT / f).read_text()
        assert '"$tmp/venv/bin/python" -I -S -m compileall' in sh
        assert 'as_user "$1/venv/bin/python" -c \'import numpy\'' in sh
    assert '"$VENV/bin/python" -S -P -m sizeup.radio.sdrserial' in (ROOT / "deploy/station/sizeup-station").read_text()


def test_a_release_with_the_adapter_that_doesnt_come_up_retries_without_it(station):
    repo, u, ad, good = _with_adapter(station)
    c1 = ad.commits[0]
    new = repo.commit("two")
    u.unhealthy_releases = {au.release_name(new, c1)}       # the adapter hangs the new release's start
    u.want(new)
    assert u.run() == 0
    assert u.current() == (new, new) and u.installed() is None             # sizeup's release runs, without it
    assert new not in u.load().get("failed", {}) and c1 in u.load()["adapter"]["failed"]
    assert u.calls[-3:] == [("activate", au.release_name(new, c1)), ("activate", good), ("activate", new)]
    assert "without the adapter" in u.p.log.read_text().splitlines()[-1]
    # (the reviewer's probe: both with and without the adapter fail: then it is sizeup's)
    newer = repo.commit("three")
    u.unhealthy.add(newer)
    u.want(newer)
    u.unhealthy_releases = set()
    assert u.run() == 1 and newer in u.load()["failed"] and u.current()[1] == new


def test_an_interrupted_start_with_the_adapter_retries_without_it(station):
    repo, u, ad, good = _with_adapter(station)
    c1 = ad.commits[0]
    new = repo.commit("two")
    u.make_release(new, new)
    target = au.release_name(new, c1)
    shutil.copytree(u.p.releases / good, u.p.releases / target, symlinks=True, dirs_exist_ok=True)
    (u.p.releases / target / "BUILD").write_text(new + "\n")
    u.switch(target)                                        # switched, then the box rebooted, and it never came up
    u.unhealthy_releases = {target}
    u.save(u.load(), state="updating", to=new, adapter_to="", **{"from": good})
    u.want(new)
    u.run()
    assert u.current() == (new, new) and c1 in u.load()["adapter"]["failed"] and new not in u.load().get("failed", {})


def test_remove_takes_the_drop_in_out_first_and_keeps_the_files_if_systemd_says_no(station):
    repo, u, ad, good = _with_adapter(station)
    _current_release_with_configupdate(u)
    u.systemctl_ok = False
    out = []
    assert au.adapter_remove(u, yes=True, out=out.append) == 1
    assert not u.p.dropin.exists() and u.cred_has("FEED_USER")           # drop-in gone, files kept
    assert "left in place" in out[-1]
    u.systemctl_ok = True
    assert au.adapter_remove(u, yes=True, out=out.append) == 0 and not u.cred_names()


def test_a_missing_credential_shows_failing_and_the_drop_in_never_names_it(station):
    repo, u, ad, good = _with_adapter(station)
    u.cred_file("FEED_USER").unlink()                       # deleted by hand
    r = u.report()["adapter"]
    assert r["state"] == "failing" and "FEED_USER missing" in r["text"]
    assert any("FEED_USER is not set" in ln for ln in au.doctor_lines(u.report()))
    assert "ImportCredential=sizeup-adapter.*" in u.p.dropin.read_text()  # 255: no per-file line to fail the start
    u.systemd = 252
    assert "doesn't match" in u.dropin_problem()                          # (Debian 12: doctor says so)


def test_rollback_force_to_an_adapter_release_that_isnt_set_up(station):
    repo, u, ad, withad = _with_adapter(station)
    s = repo.shas[0]
    u.p.adapter_key.unlink()
    u.p.adapter_pending.unlink()
    u._settings = None
    u.run()
    assert u.rollback() == 1 and u.current()[0] == s
    assert u.rollback(force=True) == 0 and u.current()[0] == withad
    assert not u.load().get("failed")
    u.calls.clear()
    u.run()                                                 # the next check takes the adapter out again
    assert u.current()[0] == s


def test_prune_keeps_only_the_wheels_of_kept_releases_and_the_ref(station):
    repo, u, ad, good = _with_adapter(station)
    for i in range(3):
        ad.commit(f"def make(ctx):\n    return []\n# {i}\n")
        assert u.run() == 0
    kept = {(u.release_adapter(r) or {}).get("commit") for r in u.releases()} | {u.load()["adapter"]["remote"]}
    have = {d.name.split("-", 1)[0] for d in u.p.adapter_wheels.iterdir()}
    assert have <= kept and ad.commits[0] not in have and len(have) <= au.KEEP_RELEASES + 1


def test_probe_a_wheel_that_wont_build_never_fails_sizeup(station):
    """(re-review probe) a new adapter commit that won't build, and a new sizeup sha: sizeup moves, never failed."""
    repo, u, ad, good = _with_adapter(station)
    ad.commit("def make(ctx):\n    return []\n# x\n")
    u.wheel_fail = True
    new = repo.commit("two")
    u.want(new)
    t = [1e9]
    u.clock = lambda: t[0]
    for _ in range(au.MAX_STAGES + 2):
        u.run()
        t[0] += 4000
    assert new not in u.load().get("failed", {}) and u.current()[1] == new
    assert u.installed()["commit"] == ad.commits[0]                       # the one that ran, from its cached wheels


def test_probe_adapter_only_wheel_failures_back_off(station):
    repo, u, ad, good = _with_adapter(station)
    ad.commit("def make(ctx):\n    return []\n")
    u.wheel_fail = True
    n0 = len(u.builds)
    for _ in range(5):
        u.run()
    assert len(u.builds) - n0 <= 1



def test_uninstall_removes_only_what_root_unpacked_never_what_the_record_says(station):
    """(final review M-a) the adapter's RECORD names another package's file: nothing outside the adapter is deleted."""
    repo, u, ad = station
    _install_other_dist(u.p.releases / repo.shas[0])
    u.extra_record = ["otherpkg/__init__.py", "../../../../etc/passwd", "/etc/hosts"]
    u.set_up(ad.bare)
    u.want(repo.shas[0])
    assert u.run() == 0
    site = u.p.current.resolve() / "venv" / SITE
    files = (site / "feed_adapter-0.1.dist-info" / au.FILES).read_text().split()
    assert "feed_adapter/__init__.py" in files and "otherpkg/__init__.py" not in files
    c2 = ad.commit("def make(ctx):\n    return []\n")
    assert u.run() == 0 and u.installed()["commit"] == c2           # the old version out (by root's list)
    site = u.p.current.resolve() / "venv" / SITE
    assert (site / "otherpkg" / "__init__.py").is_file()
    u.uninstall_dist(site, "feed-adapter")
    assert (site / "otherpkg" / "__init__.py").is_file() and not (site / "feed_adapter").exists()
    # one root didn't install (no SIZEUP-FILES): left alone
    (site / "otherpkg-1.0.dist-info" / "RECORD").write_text("otherpkg/__init__.py,,\n")
    u.uninstall_dist(site, "otherpkg")
    assert (site / "otherpkg" / "__init__.py").is_file()


def test_rollback_after_a_fallback_without_the_adapter_finds_the_old_release(station):
    """(final review M-b) T with adapter c1 didn't come up, T without it did (c1 failed on T): a rollback still goes
    back to S with c1, which ran fine."""
    repo, u, ad, good = _with_adapter(station)
    c1 = ad.commits[0]
    new = repo.commit("two")
    u.unhealthy_releases = {au.release_name(new, c1)}
    u.want(new)
    assert u.run() == 0 and u.current() == (new, new)
    assert u.load()["adapter"]["failed"][c1]["sha"] == new
    assert u.previous() == good
    assert u.rollback() == 0 and u.current()[0] == good and u.installed()["commit"] == c1


def test_dropping_a_credential_on_old_systemd_drop_in_first(station):
    repo, u, ad, good = _with_adapter(station)
    u.systemd = 252
    u.write_creds({"FEED_PASS": "p"})
    assert "sizeup-adapter.FEED_PASS:" in u.p.dropin.read_text()
    u.systemctl_ok = False
    with pytest.raises(StepError):
        u.write_creds({}, drop={"FEED_PASS"})
    assert u.cred_has("FEED_PASS")                                     # systemd said no: the file stays
    u.systemctl_ok = True
    u.write_creds({}, drop={"FEED_PASS"})
    assert not u.cred_has("FEED_PASS") and "FEED_PASS" not in u.p.dropin.read_text()


IMPORT_ORDER = r"""
import sys
sys.path.insert(0, {root!r})
import sizeup
sizeup.__path__.append({overlay!r})              # (a stand-in for #304's sizeup/sources, whose runner imports adapters)
for m in {order!r}:
    __import__(m)
import sizeup.cloud.adapters as a
assert a.Resets.__module__ == {resets!r}, a.Resets.__module__
assert a.HubRefused.__module__ == {guard!r}
print("ok")
"""


@pytest.mark.parametrize("order", [["sizeup.cloud.adapters", "sizeup.sources.runner"],
                                   ["sizeup.sources.runner", "sizeup.cloud.adapters"]])
def test_no_import_cycle_with_the_sources_package_either_way(tmp_path, order):
    """(final review H2) #304's runner imports sizeup.cloud.adapters at module level; adapters imports sizeup.sources
    only lazily: both orders, in fresh interpreters."""
    src = tmp_path / "sizeup" / "sources"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "resets.py").write_text("class Resets:\n    pass\n")
    (src / "runner.py").write_text(
        "from sizeup.cloud.adapters import credential  # noqa: F401 (as #304's may)\n"
        "from sizeup.sources.resets import Resets  # noqa: F401\n"
        "class HubRefused(RuntimeError):\n    pass\n"
        "def require_station(role):\n    if role == 'hub':\n        raise HubRefused('hub')\n")
    code = IMPORT_ORDER.format(root=str(ROOT), overlay=str(tmp_path / "sizeup"), order=order,
                               resets="sizeup.sources.resets", guard="sizeup.sources.runner")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr[-600:]


def test_adapters_imports_nothing_from_sources_at_load():
    r = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
                        "import sizeup.cloud.adapters; print(any(m.startswith('sizeup.sources') for m in sys.modules))"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "False", r.stderr[-400:]


# -- the public deploy check never shows the adapter (review of #305) -----------------------------------------------

REASON = "zq-reason-only-the-adapter-says"


def _private_repo(station, tmp_path):
    """The stand-in renamed so its short name can't be mistaken for the word "adapter"; its self-check fails with
    REASON (a list: #305's contract)."""
    repo, u, ad = station
    named = tmp_path / "zq-private-src.git"
    shutil.move(ad.bare, named)
    ad.bare = named
    return repo, u, ad


def _public_output(u, monkeypatch, capsys, hub_build: str) -> tuple[str, dict]:
    """What the hub's GET /api/station/status answers for this station, and what tools/deploy_check.py prints from it
    in GitHub Actions (stdout and stderr): (the printed text + the JSON, the model the Health page shows)."""
    from sizeup.cloud.health import StationHealth
    from tests.test_deploy_check import dc
    from tests.test_health import FakeHub, cfg as hcfg, station_status
    w = au.Watch(own=u.current()[1], run_dir="", state_path=str(u.p.state), clock=lambda: 1e9, log=lambda m: None,
                 release=u.p.current.resolve())
    w.hub_build = hub_build
    hub = FakeHub()
    hub.build, hub.last_state = hub_build, hub.now
    hub.status = station_status(build=u.current()[1], version=w.status())
    h = StationHealth(hub, hcfg(), None, None, lambda m: None, clock=lambda: hub.now)
    st = h.status()
    monkeypatch.setenv("SIZEUP_STATUS_TOKEN", "t")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(dc, "fetch", lambda *a, **k: (json.loads(json.dumps(st)), "", 200))
    capsys.readouterr()
    dc.main(["--url", "https://hub.example", "--build", hub_build, "--timeout", "0", "--every", "0"])
    out = capsys.readouterr()
    return out.out + out.err + json.dumps(st), h.model()


def _assert_private(text: str, ad, u) -> None:
    secrets = [REASON, "zq-private-src", str(ad.bare)]
    for c in ad.commits:
        secrets += [c[:7], c[:12], c]
    leaked = [x for x in secrets if x in text]
    assert not leaked, (leaked, text)
    assert "-a" + ad.commits[0][:7] not in text


@pytest.mark.parametrize("case", ["adapter-only preflight", "sizeup update, new adapter commit fails",
                                  "release with the adapter doesn't come up", "neither comes up",
                                  "adapter-only rebuild doesn't come up"])
def test_the_public_deploy_check_never_shows_the_adapters_reason_repo_or_commit(station, tmp_path, monkeypatch,
                                                                                capsys, case):
    repo, u, ad = _private_repo(station, tmp_path)
    s = repo.shas[0]
    u.set_up(ad.bare)
    u.want(s)
    assert u.run() == 0
    c1 = ad.commits[0]
    bad = f"def make(ctx):\n    return []\n\ndef self_check():\n    return [{REASON!r}, '']\n"
    hub_build = s
    if case == "adapter-only preflight":
        ad.commits.append(ad.commit(bad))
        assert u.run() == 1 and u.load()["last"]["result"] == "adapter not built"
    elif case == "sizeup update, new adapter commit fails":
        ad.commits.append(ad.commit(bad))
        hub_build = repo.commit("two")
        u.want(hub_build)
        assert u.run() == 0 and u.current()[1] == hub_build and u.load()["last"]["adapter_why"]
    elif case == "release with the adapter doesn't come up":
        hub_build = repo.commit("two")
        u.unhealthy_releases = {au.release_name(hub_build, c1)}
        u.want(hub_build)
        assert u.run() == 0 and u.current() == (hub_build, hub_build)
    elif case == "neither comes up":
        hub_build = repo.commit("two")
        u.unhealthy.add(hub_build)
        u.want(hub_build)
        assert u.run() == 1 and hub_build in u.load()["failed"]
    else:
        ad.commits.append(ad.commit("def make(ctx):\n    return []\n"))
        u.unhealthy.add(s)
        assert u.run() == 1 and "FAILED" in u.load()["last"]["line"]
    text, model = _public_output(u, monkeypatch, capsys, hub_build)
    assert "::notice" in text or "::error" in text                        # (it did print the station's word)
    _assert_private(text, ad, u)
    # the adapter's own words stay where they belong: the Health page (Access) and the station's own log
    page = json.dumps(model)
    assert "zq-private-src" in page
    if case in ("adapter-only preflight", "sizeup update, new adapter commit fails"):
        assert REASON in page and REASON in u.p.log.read_text()


def test_self_check_may_return_a_list_empty_is_a_pass(station):
    repo, u, ad = station
    ad.commits.append(ad.commit("def make(ctx):\n    return []\n\ndef self_check():\n    return []\n"))
    u.set_up(ad.bare)
    u.want(repo.shas[0])
    assert u.run() == 0 and u.installed()["commit"] == ad.commits[-1]
    bad = ad.commit("def make(ctx):\n    return []\n\ndef self_check():\n    return ['no FEED_USER', 'no FEED_PASS']\n")
    assert u.run() == 1
    assert "no FEED_USER; no FEED_PASS" in u.load()["adapter"]["failed"][bad]["why"]
