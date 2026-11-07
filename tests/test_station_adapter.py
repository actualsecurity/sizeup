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
        self.pips: list[tuple] = []

    def make_release(self, name, build, builds=""):
        super().make_release(name, build, builds)
        v = self.releases_dir / name / "venv"
        if not v.exists():
            venv.create(v, with_pip=False, symlinks=True)

    def pip_install(self, rel, src, dist, drop):
        self.pips.append(("install", rel.name, dist, tuple(drop)))
        site = rel / "venv" / SITE
        self.pip_uninstall(rel, list(drop))
        meta = tomllib.loads((src / "pyproject.toml").read_text())["project"]
        shutil.rmtree(site / "feed_adapter", ignore_errors=True)
        shutil.copytree(src / "feed_adapter", site / "feed_adapter")
        di = site / f"{dist.replace('-', '_')}-{meta['version']}.dist-info"
        shutil.rmtree(di, ignore_errors=True)
        di.mkdir()
        (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {meta['version']}\n")
        eps = meta.get("entry-points", {}).get(au.ADAPTER_GROUP, {})
        (di / "entry_points.txt").write_text(f"[{au.ADAPTER_GROUP}]\n" + "".join(f"{k} = {v}\n" for k, v in eps.items()))

    def pip_uninstall(self, rel, dists):
        if dists:
            self.pips.append(("uninstall", rel.name, tuple(dists)))
        site = rel / "venv" / SITE
        for d in dists:
            for p in list(site.glob(f"{d.replace('-', '_')}-*.dist-info")) + [site / "feed_adapter"]:
                shutil.rmtree(p, ignore_errors=True)

    def set_up(self, repo: Path, ref: str = "main") -> None:
        self.p.adapter_key.write_text("fake private key\n")
        self.p.adapter_key.chmod(0o600)
        self.p.adapter_pending.write_text(json.dumps({"repo": str(repo), "ref": ref, "env": ["FEED_USER"]}))
        self._settings = None

    def installed(self) -> dict | None:
        return self.release_adapter(self.current()[0])


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
    assert view["state"] == "loaded" and view["text"] == f"adapter {c1[:7]} loaded"
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
    assert u.load()["last"]["result"] == "adapter preflight failed"
    assert "not switched (adapter" in u.p.log.read_text().splitlines()[-1]
    assert not (u.p.releases / au.release_name(s, bad)).exists()   # nothing left half-built
    view = u.report()["adapter"]
    assert view["state"] == "failed" and f"{bad[:7]} failed" in view["text"]
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
    assert "not trying adapter" in u.p.log.read_text().splitlines()[-1]


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
    u.p.env.write_text('SIZEUP_STATION_TOKEN=x\nFEED_USER="u"\n')
    _current_release_with_configupdate(u)
    lines = []
    assert au.adapter_remove(u, yes=True, out=lines.append) == 0
    assert not u.p.adapter_key.exists() and not u.p.adapter_pending.exists() and not u.p.adapter_repo.exists()
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
    answers = iter(["git@github.com:example-org/feed-adapter.git", "", "FEED_USER, FEED_PASS"])
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
    env = u.p.env.read_text()
    assert "SIZEUP_STATION_TOKEN=tok-123" in env                              # the station's own kept
    assert 'FEED_USER="us\\"er\\$1"' in env and 'FEED_PASS="p\\\\ass word\\`x"' in env
    assert stat.S_IMODE(u.p.env.stat().st_mode) == 0o600
    cfg = tomllib.loads(u.p.config.read_text())                               # no rollback target: written now
    assert cfg["adapter"] == {"repo": "git@github.com:example-org/feed-adapter.git", "ref": "main",
                              "env": ["FEED_USER", "FEED_PASS"]}
    assert not u.p.adapter_pending.exists() and "FEED" not in u.p.config.read_text().split("env =")[0]
    u._settings = None
    assert u.adapter() == {"repo": "git@github.com:example-org/feed-adapter.git", "ref": "main",
                           "env": ["FEED_USER", "FEED_PASS"], "why": ""}
    r = u.report()["adapter"]
    assert r["env"] == [("FEED_USER", True), ("FEED_PASS", True)] and r["state"] == "pending"
    # again: Enter keeps each credential, a name dropped takes its value out
    said.clear()
    answers = iter(["", "v2", "FEED_USER"])
    assert au.adapter_setup(u, ask=lambda p: next(answers), secret=lambda p: "", out=said.append,
                            make_key=make_key, check=False) == 0
    assert len(made) == 1                                                     # the same key
    env = u.p.env.read_text()
    assert 'FEED_USER="us\\"er\\$1"' in env and "FEED_PASS" not in env
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
    assert u.pick_adapter({}, u.current()[0]) == (None, "")


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
    assert v == {"state": "loaded", "repo": "adapter", "commit": ad.commits[0][:12],
                 "text": f"adapter {ad.commits[0][:7]} loaded"}
    assert str(ad.bare) not in json.dumps(w.status())                         # the URL stays on the station
    sent = dict(v, health=[{"source": "feed", "label": "Feed", "state": "ok", "reason": "", "password": "x"}],
                url="git@x:y/z.git", env={"FEED_PASS": "x"})
    h = station_adapter(sent)
    assert set(h) == {"state", "repo", "commit", "text", "health"} and "x" not in json.dumps(h["health"])
    assert station_adapter({"state": "off"}) is None and station_adapter(None) is None
    assert au.adapter_view(None, None)["state"] == "off"


def test_the_station_link_adds_each_sources_health():
    from sizeup.cloud.station import StationLink
    rows = [{"source": "feed", "label": "Feed", "rank": 1, "state": "failing", "reason": "HTTP 503", "events": 2}]
    fake = type("L", (), {})()
    fake.updates = type("W", (), {"status": lambda self: {"adapter": {"state": "loaded", "text": "x 1234567 loaded"}}})()
    fake.app = type("A", (), {"sources_runner": type("R", (), {"state": lambda self: rows})()})()
    ad = StationLink.adapter(fake)
    assert ad["text"] == "x 1234567 loaded · Feed failing (HTTP 503)"
    assert ad["health"] == [{"source": "feed", "label": "Feed", "state": "failing", "reason": "HTTP 503"}]
    fake.app = object()                                                       # no runner (before #297): no rows
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
    assert au.adapter_repo_problem("/srv/adapter.git") == ""
    for bad in ("https://github.com/o/r", "-oProxyCommand=x", "ext::sh -c x", "git@github.com:o/r;x", ""):
        assert au.adapter_repo_problem(bad)
    assert au.repo_name("git@github.com:o/feed.git") == "feed" and au.repo_name("/srv/x/adapter.git") == "adapter"
    assert au.github_slug("git@github.com:o/feed.git") == "o/feed"
    assert au.github_slug("ssh://git@github.com/o/feed") == "o/feed" and au.github_slug("/srv/a.git") == ""
    assert au.env_quote('a"b$c`d\\e') == '"a\\"b\\$c\\`d\\\\e"'
    with pytest.raises(ValueError):
        au.env_quote("two\nlines")
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


def test_station_env_written_root_only(tmp_path):
    u = au.Updater(au.Paths(env=tmp_path / "station.env"))
    u.write_env({"FEED_USER": "a b"})
    assert stat.S_IMODE(os.stat(tmp_path / "station.env").st_mode) == 0o600
    assert u.env_has("FEED_USER") and not u.env_has("FEED_PASS")
    u.write_env({}, drop={"FEED_USER"})
    assert u.env_has("FEED_USER") is False


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
