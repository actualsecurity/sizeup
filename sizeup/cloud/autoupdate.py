"""Station auto-update (issue #58): a Linux station installed by deploy/station/install-linux.sh runs
the exact commit the hub runs, and moves to the hub's new build by itself, within about a minute,
side by side: the new release is built and checked next to the running one, then one switch.

    hub      POST /api/station/state answers {ok, build}: its SIZEUP_BUILD, the git sha CI built the
             image from (.github/workflows/deploy.yml)
    station  StationLink hands every answer to Watch.hub(). Once the hub has said the same build for
             STABLE_S (a rolling deploy answers with old and new for a while) it goes into
             /run/sizeup/update-wanted ($SIZEUP_UPDATE_DIR: the one thing the sizeup user writes for
             this). Rewritten every REFRESH_S while the hub answers; the updater ignores a want older
             than WANT_MAX_AGE_S (the hub out of reach that long: stay put). An empty or unknown build
             removes it: nothing happens.
    updater  sizeup-update.service (root, oneshot), started by sizeup-update.path the moment that file
             changes and by sizeup-update.timer as a safety net. This file, installed as
             /usr/local/lib/sizeup/autoupdate.py, run by the system python3 (stdlib only), outside the
             sizeup process: restarting sizeup can't kill it half-way.
               1. nothing to do: no want, already on it, paused, or this sha failed before
               2. git fetch into /opt/sizeup/repo.git (root's bare clone, the read-only deploy key); the
                  sha must be on origin/main (git merge-base --is-ancestor): only what the hub can run
               3. stage: that commit's own install-linux.sh --stage SHA builds
                  /opt/sizeup/releases/<sha> (the code from git; the current venv as hard links unless
                  pyproject.toml changed; trunk-recorder / whisper.cpp / rtl-sdr into builds/<pin> only
                  when a pin changed) and preflights it (imports, the config loads, the databases come
                  up in a scratch dir, the units parse, the programs run). The running station is not
                  touched; a failed preflight = no switch, the sha recorded as failed
               4. a quiet moment: no carrier open, nobody talking, no transcription queued or running
                  (the station's GET /api/station-mode, "update" -> "busy"); after QUIET_MAX_S it goes anyway
               5. activate: install-linux.sh --activate SHA: the build links and /opt/sizeup/current
                  are swapped by rename, one restart of sizeup-station (whisper-server and trunk-recorder
                  are services of their own: the model stays loaded, P25 stays on its control channel,
                  unless what they run changed). Going to a release from before trunk-recorder's own
                  service (#71), that service is stopped and removed first: its sizeup runs trunk-recorder
                  itself, and would find the dongle claimed
               6. /healthz must answer with the new build within HEALTH_S, else the same swap back to
                  the release that ran before (nothing rebuilt), and the sha is remembered as failed:
                  not tried again until the hub moves on to another one
               7. the last KEEP_RELEASES releases are kept, older ones and unused builds removed
               8. WSL: deploy/station/windows/*.ps1 differ from C:\\ProgramData\\sizeup: copied over
                  through /mnt/c, or "Windows scripts out of date: run install-windows.ps1"
             One line per attempt and result: the journal (sizeup-update) and /var/log/sizeup-update.log.
             What it did: /var/lib/sizeup-update/state.json (root's, world-readable), shown on the
             station's Hub row, by sizeup-station status / doctor and, in the station's status, on the hub.
    paused   install.conf SIZEUP_AUTO_UPDATE=off (sizeup-station autoupdate off), [station]
             auto_update = false in the config, a station installed with another --ref than main
             (testing a branch: the hub runs main), or one installed from a folder (--from-dir).
    adapter  the station's private, read-only incident-source adapter (#301; the interface: #297), kept in a repo
             of its own and never on the hub. Only with its deploy key (/etc/sizeup/adapter-deploy-key) and
             [adapter] repo in the config (sudo sizeup-station adapter setup); without them nothing here changes.
             Each update (and each run while the station is on the hub's build) fetches [adapter] ref over SSH
             into /opt/sizeup/adapter.git; the release is staged as usual (releases/<sha>), then copied (hard
             links) to releases/<sha>-a<commit12> with the adapter pip-installed into that copy's venv (the
             wheel built as the sizeup user) and preflighted: it imports, has a sizeup.incident_sources entry
             point, and its module's self_check() (if it has one) returns "" or None, run as the sizeup user
             with no credentials. A failure: no switch. A new adapter commit alone is a side-by-side rebuild of
             the same sha; a rollback goes back to the previous release, its adapter with it. A failed adapter
             commit is not tried again until the ref moves (a sizeup update meanwhile keeps the adapter that
             runs). ADAPTER.json in the release says what it has; state.json "adapter" what the updater saw.

    python3 autoupdate.py run | status | doctor | on | off | prune | rollback
    python3 autoupdate.py adapter setup | status | remove | stage RELEASE
"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STABLE_S = 30.0            # the hub says the same build this long before the station asks for it
REFRESH_S = 600.0          # the want is rewritten this often while the hub answers
WANT_MAX_AGE_S = 3600.0    # an older want (the hub out of reach): ignored
QUIET_MAX_S = 300.0        # wait this long for a quiet moment, then update anyway
QUIET_POLL_S = 5.0
HEALTH_S = 90.0            # /healthz with the new build within this after the switch, or back
RADIOS_S = 60.0            # how long to watch for the radios coming back (measured, not required)
STALE_ATTEMPT_S = 3 * 3600.0
KEEP_FAILED = 20
KEEP_RELEASES = 3
MAIN = "main"
SHA = re.compile(r"[0-9a-f]{7,40}")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
PORT = 8090                # [web] port when the config has none (sizeup/config.py's default)
PREFLIGHT_EXIT = 87        # install-linux.sh --stage: the release was built but its preflight failed (set only there)
RETRY_S = (300.0, 900.0, 3600.0, 3600.0)   # a stage that broke for another reason (apt lock, network, disk)
MAX_STAGES = 5             # ... is tried this often per sha, then recorded as failed
KEEP_EQUIVALENT = 10
# Changes here don't change what the station runs: the hub's build moves on, the station stays as it is
# (no restart) and counts as up to date. Conservative: anything not listed restarts.
NOT_RUNTIME = ("docs/", "tests/", ".github/", "deploy/hub/", "deploy/aws/", "Dockerfile", ".dockerignore",
               ".gitignore", "deploy/station/install-windows.ps1", "deploy/station/uninstall-windows.ps1")
NOT_RADIOS = ("Hub", "STT")
TR_UNIT = "sizeup-trunk-recorder.service"      # trunk-recorder's own service (#71, sizeup/radio/trunkrecd.py)
WINDOWS_STALE = "Windows scripts out of date: run install-windows.ps1"
SVC_USER = "sizeup"

# -- the incident-source adapter (#301) ---------------------------------------------------------------
ADAPTER_GROUP = "sizeup.incident_sources"   # the entry-point group adapters register in (#297: sizeup/sources)
ADAPTER_FILE = "ADAPTER.json"               # in a release built with an adapter: what its venv has
ADAPTER_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
ADAPTER_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# the station's own variables (station.env, the unit): never an adapter's credential
RESERVED_ENV = {"SIZEUP_STATION_TOKEN", "SIZEUP_ACCESS_CLIENT_ID", "SIZEUP_ACCESS_CLIENT_SECRET", "SIZEUP_BUILD",
                "SIZEUP_STATE_DIR", "SIZEUP_UPDATE_DIR", "SIZEUP_STT_EXTERNAL", "SIZEUP_TRUNK_RECORDER_UNIT",
                "SIZEUP_INCIDENT_SOURCES", "SIZEUP_INCIDENT_SOURCES_PATH", "HOME", "PATH", "USER", "SHELL", "LANG"}
RESERVED_PREFIX = ("LD_", "PYTHON", "SYSTEMD_", "GIT_", "SSH_")
ADAPTER_CHECK_S = 120.0
HUB_REFUSED = "incident-source adapters run on the station only, never on the hub"
# Run by the release's venv python, as the sizeup user, no credentials: the adapter imports and passes its self-check.
ADAPTER_CHECK = r'''
import importlib, importlib.metadata as md, json, sys
dist, role, group = sys.argv[1], sys.argv[2], sys.argv[3]
def done(ok, why="", **kw):
    print("\n" + json.dumps(dict(kw, ok=ok, why=str(why)[:300])))
    sys.exit(0 if ok else 3)
if role.strip().lower() == "hub":
    done(False, "incident-source adapters run on the station only, never on the hub")
try:
    d = md.distribution(dist)
except md.PackageNotFoundError:
    done(False, f"{dist} isn't installed")
eps = [e for e in d.entry_points if e.group == group]
if not eps:
    done(False, f"{dist} has no {group} entry point")
names = []
for e in eps:
    try:
        f = e.load()
        mod = sys.modules.get(e.module) or importlib.import_module(e.module)
    except Exception as x:
        done(False, f"{e.name}: import: {type(x).__name__}: {x}")
    if not callable(f):
        done(False, f"{e.name}: {e.value} isn't callable")
    chk = getattr(mod, "self_check", None)
    if callable(chk):
        try:
            r = chk()
        except Exception as x:
            done(False, f"{e.name}: self_check: {type(x).__name__}: {x}")
        if r not in (None, "", True):
            done(False, f"{e.name}: self_check: {r}")
    names.append(e.name)
done(True, version=d.version, sources=names)
'''


def adapter_repo_problem(url) -> str:
    """Why this isn't an adapter repo URL ("" when it is): an SSH git URL (git@host:owner/repo.git,
    ssh://git@host/owner/repo.git), or an absolute local path (a repository on this machine: CI, tests)."""
    u = str(url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._][A-Za-z0-9._/-]*", u):
        return ""
    if re.fullmatch(r"ssh://[A-Za-z0-9._-]+@[A-Za-z0-9.-]+(:[0-9]{1,5})?/[A-Za-z0-9._][A-Za-z0-9._/-]*", u):
        return ""
    if re.fullmatch(r"/[A-Za-z0-9._][A-Za-z0-9._/-]*", u):
        return ""
    return "an SSH git URL (git@github.com:owner/repo.git) or a local path"


def repo_name(url) -> str:
    """The repo's short name (no host, no owner): what status and the hub show."""
    tail = re.split(r"[/:]", str(url or "").rstrip("/"))[-1]
    return (tail[:-4] if tail.endswith(".git") else tail)[:40] or "?"


def github_slug(url) -> str:
    """owner/repo for a GitHub SSH URL ("" otherwise): for the gh command printed at setup."""
    m = re.fullmatch(r"(?:[A-Za-z0-9._-]+@github\.com:|ssh://[A-Za-z0-9._-]+@github\.com(?::[0-9]+)?/)"
                     r"([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?", str(url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def env_problem(name: str) -> str:
    if not ADAPTER_ENV.fullmatch(name):
        return f"{name!r}: A-Z, 0-9 and _, starting with a letter"
    if name in RESERVED_ENV or name.startswith(RESERVED_PREFIX):
        return f"{name}: the station's own (pick another name)"
    return ""


def env_quote(value: str) -> str:
    """A value as systemd's EnvironmentFile reads it back exactly: double quotes, \\ " ` $ escaped."""
    if any(c in value for c in "\n\r\0"):
        raise ValueError("one line, please")
    return '"' + re.sub(r'([\\"`$])', r"\\\1", value) + '"'


def release_name(sha: str, commit: str = "") -> str:
    """releases/<sha>, or releases/<sha>-a<adapter commit, 12> for one built with an adapter."""
    return f"{sha}-a{commit[:12]}" if commit else sha


def adapter_view(installed: dict | None, rec: dict | None) -> dict:
    """{state, repo, commit, text}: the adapter as status, the station page and the hub show it (no credentials, no
    URL). state: off | loaded | failed | pending. installed: the running release's ADAPTER.json; rec: state.json's."""
    rec = rec if isinstance(rec, dict) else {}
    installed = installed if isinstance(installed, dict) else None
    if installed is None and not rec.get("configured"):
        return {"state": "off", "repo": "", "commit": "", "text": ""}
    name = str(rec.get("repo") or repo_name((installed or {}).get("repo")))
    failed = rec.get("failed") if isinstance(rec.get("failed"), dict) else {}
    remote = str(rec.get("remote") or "")
    f = failed.get(remote) if remote else None
    why = (str(f.get("why") or "?") if isinstance(f, dict) else "")
    err = str(rec.get("error") or "")[:200]
    if installed is not None:
        c = str(installed.get("commit") or "")
        text = f"{name} {c[:7]} loaded"
        if why and not remote.startswith(c or "-"):
            text += f" · {remote[:7]} failed: {why[:200]}"
        if err:
            text += f" · {err}"
        return {"state": "failed" if why else "loaded", "repo": name, "commit": c[:12], "text": text}
    why = why or err
    return {"state": "failed" if why else "pending", "repo": name, "commit": "",
            "text": f"{name} not installed" + (f": {why[:200]}" if why else " yet")}


def _console_tz():
    """The console's time zone (sizeup/tz.py, #200; this file is stdlib only, so its own copy): times in the
    update lines the hub shows are in America/New_York, not the machine's zone. No tz data: the machine's."""
    try:
        return ZoneInfo("America/New_York")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


CONSOLE_TZ = _console_tz()


def _clock(t, fmt: str = "%H:%M") -> str:
    try:
        return datetime.fromtimestamp(float(t), CONSOLE_TZ).strftime(fmt) if t else "?"
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


# -- builds ------------------------------------------------------------------------------------------

def norm(build) -> str:
    """A git sha (7 to 40 hex digits), lower case; "" for anything else (empty, "dev", None)."""
    b = str(build or "").strip().lower()
    return b if SHA.fullmatch(b) else ""


def same_build(a, b) -> bool:
    """The same commit: one sha is a prefix of the other (a short sha against the hub's full one)."""
    a, b = norm(a), norm(b)
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def short(build) -> str:
    return norm(build)[:7] or "?"


def failed_entry(failed: dict | None, sha: str) -> dict | None:
    for k, v in (failed or {}).items():
        if same_build(k, sha):
            return v if isinstance(v, dict) else {"why": str(v)}
    return None


def runtime_changes(changed: list[str]) -> list[str]:
    """The changed paths the station runs ([] = docs, tests, CI, the hub's own files: no restart)."""
    return [f for f in changed if not f.startswith(NOT_RUNTIME)
            and not (f.endswith(".md") and not f.startswith(("sizeup/", "data/")))]


def equivalent_to(upd: dict | None, want: str, current: str) -> bool:
    """want differs from current only in what the station doesn't run (recorded by the updater)."""
    eq = (upd or {}).get("equivalent") if isinstance((upd or {}).get("equivalent"), dict) else {}
    return any(same_build(k, want) and same_build(v, current) for k, v in eq.items())


def failed_for(failed: dict | None, upd: dict | None, want: str) -> dict | None:
    """want failed before, or runs the same code as a build that did (equivalent: only docs etc.
    differ): a manual rollback from A holds while the hub sits on a docs-only descendant of A."""
    f = failed_entry(failed, want)
    if f is not None:
        return f
    eq = (upd or {}).get("equivalent") if isinstance((upd or {}).get("equivalent"), dict) else {}
    for k, v in eq.items():
        if same_build(k, want) and (f := failed_entry(failed, v)) is not None:
            return dict(f, why=f"the same code as {short(v)}, which failed: {f.get('why') or '?'}")
    return None


def plan(want: str, current: str, paused: str, failed: dict | None, upd: dict | None = None) -> tuple[str, str]:
    """What to do about the hub's build: (action, why). Actions: "none" (the hub's build isn't
    known), "current", "paused", "failed-before", "update"."""
    if not norm(want):
        return "none", "the hub's build isn't known"
    if same_build(want, current):
        return "current", f"on the hub's build {short(want)}"
    if equivalent_to(upd, want, current):
        return "current", f"on {short(current)}: the hub's {short(want)} changes nothing it runs"
    if paused:
        return "paused", paused
    f = failed_for(failed, upd, want)
    if f is not None:
        return "failed-before", f"{short(want)} failed before ({f.get('why') or '?'}): waiting for the hub to move on"
    return "update", f"{short(current)} -> {short(want)}"


def wait_quiet(busy: Callable[[], str], max_s: float, clock: Callable[[], float] = time.time,
               sleep: Callable[[float], None] = time.sleep, poll_s: float = QUIET_POLL_S) -> tuple[float, str]:
    """Wait until busy() says "" (quiet) or max_s passes: (seconds waited, what was still going on)."""
    t0 = clock()
    while True:
        why = busy()
        waited = clock() - t0
        if not why or waited >= max_s:
            return waited, why
        sleep(min(poll_s, max(0.0, max_s - waited)))


def paused_reason(s: dict) -> str:
    """Why auto-update is off for these settings, "" when it is on."""
    if s.get("source", "git") != "git":
        return "installed from a folder (--from-dir), not from git"
    if s.get("switch", "on") == "off":
        return "auto-update is off (sizeup-station autoupdate on)"
    if s.get("config") is False:
        return "auto-update is off in /etc/sizeup/config.toml ([station] auto_update = false)"
    if s.get("ref", MAIN) != MAIN:
        return f"this station runs --ref {s.get('ref')}, not {MAIN} (sizeup-station update --ref {MAIN} to follow the hub again)"
    return ""


def describe(build: str, hub: str, upd: dict | None, now: float | None = None) -> dict:
    """{state, text}: this station's version against the hub's, for the station's Hub row and the
    hub's Station row. state: ok | updating | failed | paused | behind | unknown."""
    upd = upd if isinstance(upd, dict) else {}
    now = time.time() if now is None else now
    win = f" · {upd['windows']}" if upd.get("windows") else ""
    if not norm(hub):
        return {"state": "unknown", "text": f"version {short(build)} (the hub's build isn't known: not updating){win}"}
    if same_build(build, hub):
        return {"state": "ok", "text": f"up to date ({short(build)}){win}"}
    if equivalent_to(upd, hub, build):
        return {"state": "ok", "text": f"up to date: {short(hub)} (no runtime change since {short(build)}){win}"}
    if (upd.get("state") in ("waiting", "updating") and same_build(upd.get("to"), hub)
            and now - float(upd.get("since") or 0) < STALE_ATTEMPT_S):
        wait = " (waiting for a quiet moment)" if upd.get("state") == "waiting" else ""
        return {"state": "updating", "text": f"updating {short(build)} -> {short(hub)}{wait}{win}"}
    f = failed_for(upd.get("failed"), upd, hub)
    if f is not None:
        return {"state": "failed", "text": f"update to {short(hub)} failed ({f.get('why') or '?'}): "
                                           f"staying on {short(build)} until the hub moves on{win}"}
    r = (upd.get("retry") or {}) if isinstance(upd.get("retry"), dict) else {}
    rt = next((v for k, v in r.items() if same_build(k, hub) and isinstance(v, dict)), None)
    if rt is not None:
        at = _clock(rt.get("next_at") or now)
        return {"state": "updating", "text": f"update to {short(hub)} not built yet (try {rt.get('n', '?')} of "
                                             f"{MAX_STAGES}: {rt.get('why') or '?'}); again after {at}{win}"}
    if upd.get("paused"):
        return {"state": "paused", "text": f"on {short(build)}, the hub runs {short(hub)}: {upd['paused']}{win}"}
    return {"state": "behind", "text": f"on {short(build)}, the hub runs {short(hub)}{win}"}


def read_state(path: Path | str) -> dict:
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _newest_failed(failed: dict, n: int) -> dict:
    def at(kv) -> float:
        v = kv[1]
        return -float(v.get("at") or 0) if isinstance(v, dict) else 0.0
    return dict(sorted(failed.items(), key=at)[:n])


# -- the station process's side ----------------------------------------------------------------------

class Watch:
    """In the sizeup process (StationLink): the hub's build from each answer -> the want file the
    root updater acts on. Never runs git, never restarts anything."""

    def __init__(self, own: str | None = None, run_dir: str | None = None,
                 state_path: str = "/var/lib/sizeup-update/state.json", clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, release: str | Path | None = None):
        self.own = norm(os.environ.get("SIZEUP_BUILD", "") if own is None else own)
        # the release this process runs (rel/sizeup/cloud/autoupdate.py): its ADAPTER.json, if it has an adapter
        self.release = Path(release) if release is not None else Path(__file__).resolve().parents[2]
        self._installed: dict | None = None
        d = os.environ.get("SIZEUP_UPDATE_DIR", "") if run_dir is None else run_dir
        self.want = Path(d) / "update-wanted" if d else None
        self.state_path = Path(state_path)
        self.clock, self.log = clock, log
        self.hub_build = ""            # the hub's build, once it held still for STABLE_S
        self._cand: str | None = None
        self._since = 0.0
        self._written: str | None = None
        self._at = 0.0
        self._read: tuple[float, dict] = (-1.0, {})

    def hub(self, build) -> None:
        """One answer from the hub: its build (None / "" = it didn't say)."""
        b, now = norm(build), self.clock()
        if b != self._cand:
            self._cand, self._since = b, now
        if now - self._since < STABLE_S:
            return
        if b != self.hub_build:
            self.hub_build = b
            if b and self.own and not same_build(b, self.own):
                self.log(f"auto-update: the hub runs {short(b)}, this station {short(self.own)}")
        if self.want is None or not self.want.parent.is_dir():
            return
        try:
            if not b:
                if self._written != "":
                    self.want.unlink(missing_ok=True)
                    self._written = ""
                return
            if b != self._written or now - self._at >= REFRESH_S:
                self.want.write_text(b + "\n")      # (closed after the write: sizeup-update.path fires)
                self._written, self._at = b, now
        except OSError as e:
            if self._written is not None:
                self.log(f"auto-update: can't write {self.want}: {e}")
            self._written = None

    def state(self) -> dict:
        """The updater's state.json, as the station reports it (the last few failures only)."""
        try:
            mtime = self.state_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime != self._read[0]:
            self._read = (mtime, read_state(self.state_path) if mtime else {})
        st = self._read[1]
        out = {k: st[k] for k in ("state", "since", "to", "from", "paused", "windows", "last", "equivalent", "retry")
               if k in st}
        out["failed"] = _newest_failed(st.get("failed") if isinstance(st.get("failed"), dict) else {}, 5)
        return out

    def adapter(self) -> dict:
        """The incident-source adapter this release runs, and what the updater last saw (adapter_view: no URL, no
        credentials)."""
        if self._installed is None:          # (a release never changes under a running process: read once)
            self._installed = read_state(self.release / ADAPTER_FILE)
        self.state()
        return adapter_view(self._installed or None, self._read[1].get("adapter"))

    def status(self) -> dict:
        """{build, hub, update, state, text, adapter}: in the station's status to the hub, and its Hub row."""
        upd = self.state()
        return dict({"build": self.own, "hub": self.hub_build, "update": upd, "adapter": self.adapter()},
                    **describe(self.own, self.hub_build, upd, self.clock()))


# -- the updater (root) --------------------------------------------------------------------------------

class StepError(RuntimeError):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code             # PREFLIGHT_EXIT: built, but the preflight failed (not worth retrying)


@dataclass
class Paths:
    opt: Path = Path("/opt/sizeup")
    want: Path = Path("/run/sizeup/update-wanted")
    work: Path = Path("/run/sizeup-update")                    # the extracted installer, the lock
    state: Path = Path("/var/lib/sizeup-update/state.json")
    log: Path = Path("/var/log/sizeup-update.log")
    install_conf: Path = Path("/etc/sizeup/install.conf")
    config: Path = Path("/etc/sizeup/config.toml")
    deploy_key: Path = Path("/etc/sizeup/deploy_key")
    known_hosts: Path = Path("/etc/sizeup/known_hosts")
    windows: Path = Path("/mnt/c/ProgramData/sizeup")
    systemd: Path = Path("/etc/systemd/system")
    adapter_key: Path = Path("/etc/sizeup/adapter-deploy-key")          # the adapter repo's read-only deploy key
    adapter_pending: Path = Path("/etc/sizeup/adapter-pending.json")    # setup's [adapter], until the config has it
    env: Path = Path("/etc/sizeup/station.env")
    marks: Path = Path("/etc/sizeup/config-updates")

    @property
    def adapter_repo(self) -> Path:
        return self.opt / "adapter.git"

    @property
    def repo(self) -> Path:
        return self.opt / "repo.git"

    @property
    def releases(self) -> Path:
        return self.opt / "releases"

    @property
    def current(self) -> Path:
        return self.opt / "current"

    @property
    def builds(self) -> Path:
        return self.opt / "builds"


def read_kv(path: Path) -> dict:
    """install.conf: KEY=value lines (written by install-linux.sh), read without sourcing."""
    out = {}
    try:
        for line in path.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and re.fullmatch(r"[A-Z_]+", k.strip()):
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def set_kv(path: Path, key: str, value: str) -> None:
    lines, done = [], False
    try:
        lines = path.read_text().splitlines()
    except OSError:
        pass
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i], done = f"{key}={value}", True
    if not done:
        lines.append(f"{key}={value}")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.chmod(0o644)
    tmp.replace(path)


def same_text(a: Path, b: Path) -> bool:
    """The same script, line endings aside (a copy made on Windows may have CRLF)."""
    try:
        return a.read_bytes().replace(b"\r\n", b"\n") == b.read_bytes().replace(b"\r\n", b"\n")
    except OSError:
        return False


def prune_plan(releases: list[str], history: list[str], current: str, keep: int = KEEP_RELEASES) -> list[str]:
    """The releases to remove: all but the current one and the most recently switched-to others,
    keep in all (the newest first in history; a release never switched to counts as oldest)."""
    kept: list[str] = []
    for name in [current, *reversed(history)]:
        if name and name in releases and name not in kept:
            kept.append(name)
        if len(kept) >= keep:
            break
    return [r for r in releases if r not in kept]


class Updater:
    def __init__(self, paths: Paths | None = None, *, quiet_max_s: float = QUIET_MAX_S, health_s: float = HEALTH_S,
                 radios_s: float = RADIOS_S, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, out: Callable[[str], None] | None = None):
        self.p = paths or Paths()
        self.quiet_max_s, self.health_s, self.radios_s = quiet_max_s, health_s, radios_s
        self.clock, self.sleep = clock, sleep
        self.out = out or (lambda line: print(line, flush=True))
        self._settings: dict | None = None
        self._line = ""                 # the last result() line: saved with "last" (the hub's deploy check prints it)

    # -- the outside world (tests replace these) ---------------------------------------------------------

    def git(self, *args: str, check: bool = True) -> str:
        """git in /opt/sizeup/repo.git, as root (root's clone, root's copy of the deploy key)."""
        ssh = (f"ssh -i {self.p.deploy_key} -o IdentitiesOnly=yes -o UserKnownHostsFile={self.p.known_hosts} "
               "-o StrictHostKeyChecking=yes -o BatchMode=yes")
        r = subprocess.run(["git", "--no-replace-objects", f"--git-dir={self.p.repo}", *args], capture_output=True,
                           text=True, timeout=600, env=dict(os.environ, GIT_SSH_COMMAND=ssh))
        if check and r.returncode != 0:
            lines = (r.stderr or r.stdout).strip().splitlines()
            raise StepError(f"git {args[0]}: {lines[-1] if lines else 'failed'}")
        return r.stdout.strip() if r.returncode == 0 else ""

    def installer(self, args: list[str], script: Path) -> None:
        """bash SCRIPT ARGS, its output passed through (the journal); StepError with its last
        FAILED / WARNING lines when it fails."""
        tail: deque[str] = deque(maxlen=40)
        # (this run holds the update lock: the installer must not take it again)
        proc = subprocess.Popen(["bash", str(script), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", env=dict(os.environ, SIZEUP_UPDATE_LOCKED="1"))
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            self.out(line)
        if proc.wait() != 0:
            said = [ln.split("  ", 1)[-1] for ln in tail if "FAILED" in ln or "WARNING" in ln]
            raise StepError("; ".join(said[-3:]) or f"{script.name} {args[0]} failed (exit {proc.returncode})",
                            proc.returncode)

    def stage(self, sha: str) -> None:
        """That commit's own installer builds and preflights releases/<sha> (nothing switches)."""
        self.p.work.mkdir(parents=True, exist_ok=True)
        script = self.p.work / "install-linux.sh"
        script.write_text(self.git("show", f"{sha}:deploy/station/install-linux.sh") + "\n")
        script.chmod(0o700)
        self.installer(["--stage", sha], script)

    def systemctl(self, *args: str) -> None:
        try:
            r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            self.note(f"auto-update: systemctl {' '.join(args)}: {type(e).__name__}: {e}")
            return
        if r.returncode != 0:
            self.note(f"auto-update: systemctl {' '.join(args)}: {(r.stderr or r.stdout).strip()[-200:]}")

    def before_switch(self, name: str) -> None:
        """A release from before #71 runs trunk-recorder inside sizeup-station, and its installer doesn't know the
        service: stop, disable and remove it before that release's sizeup starts (the service holds the dongle's claim,
        and that sizeup would leave Fire/EMS 15 Off rather than fight for it). Switching forward again installs it."""
        unit = self.p.systemd / TR_UNIT
        rel = self.p.releases / name
        if not unit.exists() or not rel.is_dir() or (rel / "deploy" / "station" / "systemd" / TR_UNIT).is_file():
            return
        b = self.build_of(name)
        self.note(f"auto-update: release {short(b) if norm(b) else name} runs trunk-recorder inside sizeup-station: "
                  f"{TR_UNIT} stopped and removed")
        self.systemctl("disable", "--now", TR_UNIT)
        try:
            unit.unlink()
        except OSError as e:
            self.note(f"auto-update: {unit} not removed: {e}")
        self.systemctl("daemon-reload")

    def activate(self, name: str, leaving: str = "") -> None:
        """install-linux.sh --activate NAME: links swapped, one restart. The release's own installer;
        one from before side-by-side releases has none that can, so the one we leave does it."""
        self.before_switch(name)
        for cand in (name, leaving, *reversed(self.history())):
            script = self.p.releases / cand / "deploy/station/install-linux.sh" if cand else None
            if script is not None and script.is_file() and "--activate)" in script.read_text(errors="replace"):
                self.installer(["--activate", name], script)
                return
        raise StepError(f"no installer that can switch to {name}")

    def _get(self, path: str, timeout: float = 5.0) -> dict | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.settings()['port']}{path}", timeout=timeout) as r:
                d = json.loads(r.read(16 * 1024 * 1024) or b"{}")      # (the sizeup process answers: bounded)
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def busy(self) -> str:
        """What the station is in the middle of ("" = quiet, or it doesn't answer / doesn't say)."""
        d = self._get("/api/station-mode") or {}
        upd = d.get("update") if isinstance(d.get("update"), dict) else {}
        return str(upd.get("busy") or "")

    def radios_ok(self) -> set[str] | None:
        """The radio rows that are "ok" right now (None: the station doesn't answer)."""
        d = self._get("/api/state", timeout=10.0)
        if d is None:
            return None
        return {str(r.get("label")) for r in d.get("radios") or [] if isinstance(r, dict) and r.get("state") == "ok"
                and r.get("label") not in NOT_RADIOS and "voice" not in str(r.get("label")).lower()}

    def healthy(self, sha: str) -> str:
        """"" once /healthz answers with this build; else why not, after health_s."""
        t0, said = self.clock(), ""
        while self.clock() - t0 < self.health_s:
            h = self._get("/healthz", timeout=3.0)
            if h is not None:
                if same_build(h.get("build"), sha):
                    return ""
                said = str(h.get("build") or "none")
            self.sleep(1.0)
        if said:
            return f"/healthz said build {short(said)}, not {short(sha)}, for {self.health_s:g} s"
        return f"/healthz didn't answer within {self.health_s:g} s of the switch"

    def radios_back(self, before: set[str] | None, t0: float) -> float | None:
        """Seconds from t0 until every radio that was ok before the switch is ok again (None: not
        within radios_s, or nothing to watch)."""
        if not before:
            return None
        while self.clock() - t0 < self.radios_s:
            now = self.radios_ok()
            if now is not None and before <= now:
                return self.clock() - t0
            self.sleep(1.0)
        return None

    def is_wsl(self) -> bool:
        try:
            return bool(re.search("microsoft|wsl", Path("/proc/sys/kernel/osrelease").read_text(), re.I))
        except OSError:
            return False

    # -- settings, releases and state -----------------------------------------------------------------------

    def settings(self) -> dict:
        if self._settings is None:
            kv = read_kv(self.p.install_conf)
            try:
                cfg = tomllib.loads(self.p.config.read_text())
            except (OSError, tomllib.TOMLDecodeError):
                cfg = {}
            station, web = cfg.get("station") or {}, cfg.get("web") or {}
            cloud, adapter = cfg.get("cloud") or {}, cfg.get("adapter") or {}
            self._settings = {"ref": kv.get("SIZEUP_REF", MAIN), "source": kv.get("SIZEUP_SOURCE", "git"),
                              "switch": kv.get("SIZEUP_AUTO_UPDATE", "on"),
                              "config": station.get("auto_update", True) if isinstance(station, dict) else True,
                              "port": int(web.get("port", PORT)) if isinstance(web, dict) else PORT,
                              "role": str(cloud.get("role") or "") if isinstance(cloud, dict) else "",
                              "adapter": adapter if isinstance(adapter, dict) else {}}
        return self._settings

    # -- the incident-source adapter (#301) ----------------------------------------------------------------------

    def adapter(self) -> dict:
        """The adapter this station is set up for: {repo, ref, env, why}; {} = none (no key and no repo: nothing
        changes). why: set up, but not usable ("" = go). setup's values (adapter-pending.json) until the config has
        them (configupdate.py: [adapter] waits while a rollback would go to a release that can't read it)."""
        s = self.settings()
        a = read_state(self.p.adapter_pending) or s["adapter"]
        repo, ref = str(a.get("repo") or "").strip(), str(a.get("ref") or MAIN).strip()
        env = [str(x) for x in a.get("env") or [] if isinstance(x, str)] if isinstance(a.get("env"), list) else []
        key = self.p.adapter_key.is_file()
        if not key and not repo:
            return {}
        out = {"repo": repo, "ref": ref, "env": env, "why": ""}
        if str(s.get("role") or "").strip().lower() == "hub":
            out["why"] = HUB_REFUSED
        elif not key:
            out["why"] = f"no deploy key ({self.p.adapter_key}): sudo sizeup-station adapter setup"
        elif not repo:
            out["why"] = "no [adapter] repo: sudo sizeup-station adapter setup"
        elif adapter_repo_problem(repo):
            out["why"] = f"[adapter] repo: {adapter_repo_problem(repo)}"
        elif not ADAPTER_REF.fullmatch(ref):
            out["why"] = "[adapter] ref: a branch or tag name"
        return out

    def key_open(self) -> str:
        """Why the adapter's deploy key is readable by others ("" when it is root's alone, or not there)."""
        try:
            st = self.p.adapter_key.stat()
        except OSError:
            return ""
        if st.st_mode & 0o077 or (os.geteuid() == 0 and st.st_uid != 0):
            return f"{self.p.adapter_key} is {stat.filemode(st.st_mode)} uid {st.st_uid} (want root 600)"
        return ""

    def adapter_ssh(self) -> str:
        return (f"ssh -i {self.p.adapter_key} -o IdentitiesOnly=yes -o UserKnownHostsFile={self.p.known_hosts} "
                "-o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=20")

    def adapter_git(self, *args: str, check: bool = True, timeout: float = 300) -> str:
        """git in /opt/sizeup/adapter.git, as root, over SSH with the adapter's own deploy key."""
        r = subprocess.run(["git", "--no-replace-objects", f"--git-dir={self.p.adapter_repo}", *args], capture_output=True,
                           text=True, timeout=timeout, env=dict(os.environ, GIT_SSH_COMMAND=self.adapter_ssh(),
                                                                GIT_TERMINAL_PROMPT="0"))
        if check and r.returncode != 0:
            lines = (r.stderr or r.stdout).strip().splitlines()
            raise StepError(f"adapter git {args[0]}: {lines[-1] if lines else 'failed'}")
        return r.stdout.strip() if r.returncode == 0 else ""

    def fetch_adapter(self, ad: dict) -> str:
        """The commit [adapter] ref names on its repo now (fetched into /opt/sizeup/adapter.git). StepError if not."""
        if not (self.p.adapter_repo / "HEAD").is_file():
            self.p.adapter_repo.mkdir(parents=True, exist_ok=True)
            self.adapter_git("init", "-q", "--bare")
        try:
            self.adapter_git("fetch", "-q", "--force", "--no-tags", "--", ad["repo"], f"+{ad['ref']}:refs/sizeup/adapter")
        except subprocess.TimeoutExpired:
            raise StepError("adapter git fetch: timed out") from None
        sha = self.adapter_git("rev-parse", "--verify", "--quiet", "refs/sizeup/adapter^{commit}", check=False)
        if not FULL_SHA.fullmatch(sha):
            raise StepError(f"adapter: no commit at {ad['ref']}")
        return sha

    def adapter_reach(self, ad: dict) -> str:
        """"" when the adapter's repo answers with its key (git ls-remote), else what it said."""
        if not ad or ad.get("why"):
            return (ad or {}).get("why") or "not set up"
        try:
            r = subprocess.run(["git", "ls-remote", "--", ad["repo"], ad["ref"]], capture_output=True, text=True,
                               timeout=60, env=dict(os.environ, GIT_SSH_COMMAND=self.adapter_ssh(), GIT_TERMINAL_PROMPT="0"))
        except (OSError, subprocess.SubprocessError) as e:
            return f"{type(e).__name__}: {e}"
        if r.returncode != 0:
            lines = [ln for ln in r.stderr.strip().splitlines() if ln.strip()]
            return lines[-1] if lines else f"git ls-remote: exit {r.returncode}"
        return "" if r.stdout.strip() else f"no {ad['ref']} on the repo"

    def release_adapter(self, name: str) -> dict | None:
        """What release NAME has installed (its ADAPTER.json), None when it has no adapter."""
        d = read_state(self.p.releases / name / ADAPTER_FILE) if name else {}
        return d if d.get("commit") else None

    def adapter_rec(self, st: dict) -> dict:
        rec = st.get("adapter")
        if not isinstance(rec, dict):
            rec = st["adapter"] = {}
        for k in ("failed",):
            if not isinstance(rec.get(k), dict):
                rec[k] = {}
        return rec

    def as_user(self) -> list[str]:
        """The adapter's code (its build, import and self-check) never runs as root: as the sizeup user."""
        if os.geteuid() != 0:
            return []
        return ["runuser", "-u", SVC_USER, "--"]

    def pip_install(self, rel: Path, src: Path, dist: str, drop: list[str]) -> None:
        """The adapter into rel's venv: its wheel built as the sizeup user (none of its code runs as root), then
        installed; the adapters it replaces (drop: other names installed before) uninstalled first."""
        py = str(rel / "venv" / "bin" / "python")
        wheels = Path(self.p.work) / f"adapter-wheel-{os.getpid()}"
        shutil.rmtree(wheels, ignore_errors=True)
        wheels.mkdir(parents=True)
        try:
            if os.geteuid() == 0:             # (the sizeup user builds it: its own copy of the source)
                pw = pwd.getpwnam(SVC_USER)
                for p in (wheels, src, *src.rglob("*")):
                    os.lchown(p, pw.pw_uid, pw.pw_gid)
            self._run_out([*self.as_user(), "env", f"HOME={wheels}", py, "-m", "pip", "wheel", "-q", "--no-deps",
                           "--no-cache-dir", "--disable-pip-version-check", "-w", str(wheels), str(src)],
                          "adapter: wheel", cwd=str(wheels))
            got = sorted(wheels.glob("*.whl"))
            if len(got) != 1:
                raise StepError(f"adapter: wheel: {len(got)} wheels built, not 1")
            if drop:
                self._run_out([py, "-m", "pip", "uninstall", "-y", "-q", "--disable-pip-version-check", *drop],
                              "adapter: uninstall")
            self._run_out([py, "-m", "pip", "install", "-q", "--no-cache-dir", "--disable-pip-version-check",
                           "--upgrade", "--upgrade-strategy", "only-if-needed", str(got[0])], "adapter: pip install")
        finally:
            shutil.rmtree(wheels, ignore_errors=True)

    def pip_uninstall(self, rel: Path, dists: list[str]) -> None:
        """The adapters a release's venv may have from the venv it was linked from: out (none wanted)."""
        if dists:
            self._run_out([str(rel / "venv" / "bin" / "python"), "-m", "pip", "uninstall", "-y", "-q",
                           "--disable-pip-version-check", *dists], "adapter: uninstall")

    def self_check(self, rel: Path, dist: str) -> dict:
        """The adapter imports in that release, as the sizeup user, with no credentials: {version, sources}.
        StepError(PREFLIGHT_EXIT) when it doesn't."""
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/var/lib/sizeup" if os.geteuid() == 0 else str(rel),
               "PYTHONPATH": str(rel), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        try:
            r = subprocess.run([*self.as_user(), "env", "-i", *(f"{k}={v}" for k, v in env.items()),
                                str(rel / "venv" / "bin" / "python"), "-c", ADAPTER_CHECK, dist,
                                self.settings().get("role") or "station", ADAPTER_GROUP],
                               capture_output=True, text=True, timeout=ADAPTER_CHECK_S, cwd=str(rel))
        except subprocess.TimeoutExpired:
            raise StepError(f"adapter self-check: no answer in {ADAPTER_CHECK_S:g} s", PREFLIGHT_EXIT) from None
        except OSError as e:
            raise StepError(f"adapter self-check: {e}", PREFLIGHT_EXIT) from None
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        try:
            d = json.loads(lines[-1]) if lines else {}
        except ValueError:
            d = {}
        if r.returncode != 0 or not d.get("ok"):
            why = d.get("why") or ((r.stderr or "").strip().splitlines() or ["failed"])[-1]
            raise StepError(f"adapter self-check: {why}"[:300], PREFLIGHT_EXIT)
        return {"version": str(d.get("version") or "")[:40], "sources": [str(x)[:40] for x in d.get("sources") or []][:20]}

    def _run_out(self, argv: list[str], what: str, cwd: str | None = None) -> None:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=900, cwd=cwd)
        except (OSError, subprocess.SubprocessError) as e:
            raise StepError(f"{what}: {type(e).__name__}: {e}") from None
        if r.returncode != 0:
            lines = [ln for ln in (r.stderr or r.stdout or "").strip().splitlines() if ln.strip()]
            raise StepError(f"{what}: {lines[-1] if lines else f'exit {r.returncode}'}"[:300])

    def adapter_dist(self, src: Path) -> str:
        """The adapter's distribution name: [project] name in its pyproject.toml."""
        try:
            name = tomllib.loads((src / "pyproject.toml").read_text()).get("project", {}).get("name", "")
        except (OSError, tomllib.TOMLDecodeError, AttributeError) as e:
            raise StepError(f"adapter: pyproject.toml: {e}", PREFLIGHT_EXIT) from None
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", name):
            raise StepError("adapter: no [project] name in its pyproject.toml", PREFLIGHT_EXIT)
        return name

    def build_adapter(self, base: str, ad: dict, commit: str, st: dict) -> str:
        """releases/<base>-a<commit12>: releases/<base> as hard links, the adapter at commit pip-installed into its
        venv, then its self-check. The name; StepError (PREFLIGHT_EXIT: it installed, but doesn't work) if not."""
        name = release_name(base, commit)
        rel, tmp = self.p.releases / name, self.p.releases / f".staging-{name}"
        if (rel / ".complete").is_file() and (self.release_adapter(name) or {}).get("commit") == commit:
            self.note(f"auto-update: release {name}: already staged")
            return name
        src = Path(self.p.work) / f"adapter-src-{commit[:12]}"
        for d in (tmp, rel, src):
            shutil.rmtree(d, ignore_errors=True)
        try:
            self._run_out(["cp", "-al", str(self.p.releases / base), str(tmp)], "adapter: copy the release")
            for f in tmp.iterdir():               # (the release's own small files: copies, never shared)
                if f.is_file() and not f.is_symlink():
                    data = f.read_bytes()
                    f.unlink()
                    f.write_bytes(data)
            (tmp / ".complete").unlink(missing_ok=True)
            src.mkdir(parents=True)
            arch = subprocess.run(["git", f"--git-dir={self.p.adapter_repo}", "archive", "--format=tar", commit],
                                  capture_output=True, timeout=300)
            if arch.returncode != 0:
                raise StepError(f"adapter: git archive {commit[:12]}: {arch.stderr.decode(errors='replace').strip()[-200:]}")
            subprocess.run(["tar", "-x", "-C", str(src)], input=arch.stdout, check=True, capture_output=True, timeout=300)
            dist = self.adapter_dist(src)
            rec = self.adapter_rec(st)
            drop = [d for d in rec.get("dists") or [] if isinstance(d, str) and d != dist]
            self.note(f"auto-update: release {name}: adapter {repo_name(ad['repo'])} {commit[:7]} ({dist}) into its venv")
            self.pip_install(tmp, src, dist, drop)
            rec["dists"] = sorted({*[d for d in rec.get("dists") or [] if isinstance(d, str)], dist})[-10:]
            got = self.self_check(tmp, dist)
            (tmp / ADAPTER_FILE).write_text(json.dumps(
                {"repo": ad["repo"], "ref": ad["ref"], "commit": commit, "dist": dist, "at": self.clock(), **got},
                indent=1, sort_keys=True) + "\n")
            (tmp / ADAPTER_FILE).chmod(0o644)
            os.replace(tmp, rel)
            (rel / ".complete").touch()
        except subprocess.SubprocessError as e:
            raise StepError(f"adapter: {type(e).__name__}: {e}"[:300]) from None
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(tmp, ignore_errors=True)
        self.note(f"auto-update: release {name}: adapter preflight ok ({', '.join(got['sources'])})")
        return name

    def clean_base(self, base: str, st: dict) -> None:
        """No adapter wanted: the release's venv (linked from one that had one) without it."""
        dists = [d for d in self.adapter_rec(st).get("dists") or [] if isinstance(d, str)]
        if dists and base != self.current()[0]:
            self.pip_uninstall(self.p.releases / base, dists)

    def adapter_check(self, st: dict, name: str, build: str) -> int:
        """On the hub's build already: a new adapter commit (or one set up, or removed) is a side-by-side rebuild of
        the same sha. Nothing without an adapter, as before."""
        ad, have = self.adapter(), self.release_adapter(name)
        if not ad and have is None:
            return 0
        if not FULL_SHA.fullmatch(build or ""):
            return 0
        rec = self.adapter_rec(st)
        if ad.get("why") and ad.get("repo") and self.p.adapter_key.is_file() and \
                str(self.settings().get("role") or "").lower() != "hub":
            if rec.get("error") != ad["why"]:
                self.save(st, adapter=dict(rec, error=ad["why"], configured=True))
                self.note(f"auto-update: adapter: {ad['why']}")
            return 0
        want = None if ad.get("why") or not ad else ad
        if want is None:
            if have is None:
                return 0
            return self.update(st, name, build, build, adapter=(None, ""))
        try:
            commit = self.fetch_adapter(want)
        except StepError as e:
            if rec.get("error") != str(e):
                self.save(st, adapter=dict(rec, error=str(e)[:300], configured=True, repo=repo_name(want["repo"]),
                                           ref=want["ref"]))
                self.note(f"auto-update: {e}: staying as it is")
            return 0
        rec.update(configured=True, repo=repo_name(want["repo"]), ref=want["ref"], remote=commit, error="",
                   checked=self.clock())
        if have is not None and have.get("commit") == commit and have.get("repo") == want["repo"]:
            self.save(st)
            return 0
        if commit in rec["failed"]:
            self.save(st)
            return 0
        return self.update(st, name, build, build, adapter=(want, commit))

    def pick_adapter(self, st: dict, current: str) -> tuple[dict | None, str]:
        """(settings, commit) for a new release: the commit at the ref now, else (it failed before, or can't be
        fetched) the one that runs; (None, "") without an adapter."""
        ad = self.adapter()
        if not ad or ad.get("why"):
            return None, ""
        have = self.release_adapter(current)
        known = have.get("commit", "") if have is not None and have.get("repo") == ad["repo"] else ""
        rec = self.adapter_rec(st)
        try:
            commit = self.fetch_adapter(ad)
            rec.update(configured=True, repo=repo_name(ad["repo"]), ref=ad["ref"], remote=commit, error="",
                       checked=self.clock())
        except StepError as e:
            rec.update(configured=True, repo=repo_name(ad["repo"]), ref=ad["ref"], error=str(e)[:300])
            self.note(f"auto-update: {e}" + (f": keeping adapter {known[:7]}" if known else ": built without it"))
            return (ad, known) if known else (None, "")
        if commit in rec["failed"]:
            return (ad, known) if known else (None, "")
        return ad, commit

    def env_has(self, key: str) -> bool | None:
        """Whether station.env sets key (None: it can't be read here). Names only: a value is never read out."""
        try:
            text = self.p.env.read_text()
        except OSError:
            return None
        return any(ln.startswith(f"{key}=") and ln[len(key) + 1:].strip() not in ("", '""', "''")
                   for ln in text.splitlines())

    def write_env(self, values: dict[str, str], drop: set[str] = frozenset()) -> None:
        """station.env with these set (and drop's taken out), everything else in it kept: root 0600, one rename."""
        try:
            lines = self.p.env.read_text().splitlines()
        except FileNotFoundError:
            lines = ["# sizeup station secrets (install-linux.sh / sizeup-station secrets / adapter setup). root 0600."]
        out, done = [], set()
        for ln in lines:
            k = ln.split("=", 1)[0].strip() if "=" in ln and not ln.lstrip().startswith("#") else ""
            if k in drop:
                continue
            if k in values:
                if k not in done:
                    out.append(f"{k}={env_quote(values[k])}")
                    done.add(k)
                continue
            out.append(ln)
        out += [f"{k}={env_quote(v)}" for k, v in values.items() if k not in done]
        tmp = self.p.env.with_name(f".{self.p.env.name}.{os.getpid()}")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, ("\n".join(out) + "\n").encode())
            if os.geteuid() == 0:
                os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        os.replace(tmp, self.p.env)

    def configupdate(self, *extra: str) -> tuple[int, str]:
        """The running release's configupdate.py on the config: [adapter] from adapter-pending.json now, unless a
        rollback would go to a release that can't read it (then at the next update), or extra (--remove-adapter)."""
        script = self.p.current / "sizeup" / "cloud" / "configupdate.py"
        if not script.is_file():
            return 1, f"no {script}"
        prev = self.previous()
        leaving = str(self.p.releases / prev) if prev else ""
        r = subprocess.run([sys.executable, "-I", str(script), str(self.p.config), str(self.p.marks), "--leaving", leaving,
                            *extra], capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout + r.stderr).strip()

    def adapter_failed(self, st: dict, commit: str, why: str) -> None:
        rec = self.adapter_rec(st)
        rec["failed"][commit] = {"at": self.clock(), "why": why[:300]}
        rec["failed"] = _newest_failed(rec["failed"], KEEP_FAILED)

    def current(self) -> tuple[str, str]:
        """(release name, its build) /opt/sizeup/current points at; ("", "") before releases."""
        if not self.p.current.is_symlink():
            return "", ""
        rel = Path(os.path.realpath(self.p.current))
        try:
            build = (rel / "BUILD").read_text().split()[0]
        except (OSError, IndexError):
            build = ""
        return rel.name, build

    def history(self) -> list[str]:
        try:
            return [ln.strip() for ln in (self.p.releases / ".history").read_text().splitlines() if ln.strip()]
        except OSError:
            return []

    def releases(self) -> list[str]:
        try:
            return sorted(d.name for d in self.p.releases.iterdir() if d.is_dir() and (d / ".complete").is_file())
        except OSError:
            return []

    def previous(self) -> str:
        """The release that ran before the current one (a rollback's target; never one that failed),
        "" if none is left."""
        cur, have = self.current()[0], set(self.releases())
        st = self.load()
        failed = st.get("failed") or {}
        afailed = (st.get("adapter") or {}).get("failed") if isinstance(st.get("adapter"), dict) else {}
        for name in reversed(self.history()):
            if name != cur and name in have and failed_entry(failed, self.build_of(name)) is None \
                    and (self.release_adapter(name) or {}).get("commit") not in (afailed or {}):
                return name
        return ""

    def build_of(self, name: str) -> str:
        try:
            return (self.p.releases / name / "BUILD").read_text().split()[0]
        except (OSError, IndexError):
            return ""

    def load(self) -> dict:
        return read_state(self.p.state)

    def save(self, st: dict, **kw) -> None:
        if isinstance(kw.get("last"), dict) and self._line:
            kw["last"] = dict(kw["last"], line=self._line[:500])
        st.update(kw)
        if "state" in kw:
            st["since"] = self.clock()
        self.p.state.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.p.state.with_name(".state.json.tmp")
        tmp.write_text(json.dumps(st, indent=1, sort_keys=True) + "\n")
        tmp.chmod(0o644)
        tmp.replace(self.p.state)

    def note(self, line: str) -> None:
        """Progress: the journal only."""
        self.out(line)

    def result(self, line: str) -> None:
        """The one line per attempt: the journal and /var/log/sizeup-update.log (and state.json's "last")."""
        self._line = line
        self.out(line)
        try:
            with self.p.log.open("a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.clock()))}  {line}\n")
        except OSError:
            pass

    def read_want(self) -> str:
        """The hub's build as the station last heard it; "" if none, malformed or too old. The sizeup
        user writes that file: no symlink followed, no FIFO waited on, 64 bytes at most."""
        try:
            fd = os.open(self.p.want, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return ""
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return ""
            words = os.read(fd, 64).decode("ascii", "replace").split()
        except OSError:
            return ""
        finally:
            os.close(fd)
        if time.time() - st.st_mtime > WANT_MAX_AGE_S or not words:
            return ""
        return norm(words[0])

    def retry_later(self, st: dict, sha: str) -> bool:
        """A stage of sha broke for a reason worth retrying, and its time hasn't come yet."""
        r = (st.get("retry") or {}).get(sha) if isinstance(st.get("retry"), dict) else None
        return isinstance(r, dict) and self.clock() < float(r.get("next_at") or 0)

    def _attempt(self, st: dict, sha: str) -> int:
        """Count a stage of sha before it starts (a run killed half-way counts too): the n-th try."""
        retry = st.get("retry") if isinstance(st.get("retry"), dict) else {}
        retry = {k: v for k, v in retry.items() if k == sha}          # only the hub's current build
        n = int((retry.get(sha) or {}).get("n") or 0) + 1
        retry[sha] = {"n": n, "next_at": self.clock() + RETRY_S[min(n, len(RETRY_S)) - 1],
                      "why": (retry.get(sha) or {}).get("why", "")}
        self.save(st, retry=retry)
        return n

    def _failed(self, st: dict, sha: str, why: str) -> None:
        failed = st.get("failed") if isinstance(st.get("failed"), dict) else {}
        failed[sha] = {"at": self.clock(), "why": why[:300]}
        st["failed"] = _newest_failed(failed, KEEP_FAILED)

    # -- the work -----------------------------------------------------------------------------------------------

    def locked(self, fn: Callable[[], int]) -> int:
        if os.environ.get("SIZEUP_UPDATE_LOCKED") == "1":     # (run by an installer that holds it)
            return fn()
        self.p.work.mkdir(parents=True, exist_ok=True)
        with (self.p.work / "lock").open("w") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.note("auto-update: another run is in progress")
                return 0
            return fn()

    def run(self) -> int:
        rc = 0
        for _ in range(3):              # the hub moved again while this ran: once more
            want_before = self.read_want()
            rc = self.locked(self._run)
            if self.read_want() == want_before:
                break
        return rc

    def _run(self) -> int:
        st = self.load()
        if st.get("state") in ("waiting", "updating"):
            self._interrupted(st)
        win = self.windows()
        if win != st.get("windows", ""):
            self.save(st, windows=win)
        name, build = self.current()
        want = self.read_want()
        paused = paused_reason(self.settings())
        if not name and not paused:
            paused = "this station isn't on side-by-side releases yet: sudo sizeup-station update"
        action, why = plan(want, build, paused, st.get("failed"), st)
        if action == "none":
            # the hub's build unknown: the release that runs stays, but its adapter follows its ref (#301)
            return self.adapter_check(st, name, build) if name and not paused else 0
        if action == "update" and not FULL_SHA.fullmatch(want):
            action = "paused"
            why = paused = f"the hub's build {want} isn't a full git sha: not updating"
        if action == "update" and self.retry_later(st, want):
            return 0
        if action == "current":
            if st.get("state") != "current" or st.get("current") != build or st.get("paused") != paused:
                self.save(st, state="current", current=build, hub=want, paused=paused)
            return self.adapter_check(st, name, build) if not paused else 0
        if action in ("paused", "failed-before"):
            if st.get("state") != action or not same_build(st.get("hub"), want) or st.get("paused") != paused:
                self.save(st, state=action, current=build, hub=want, paused=paused)
                self.note(f"auto-update: not updating to {short(want)}: {why}")
            # staying on this build: its adapter still follows its ref (#301); paused: nothing moves
            return self.adapter_check(st, name, build) if action == "failed-before" else 0
        return self.update(st, name, build, want)

    def _interrupted(self, st: dict) -> None:
        """A run that stopped half-way (reboot, killed): keep the new release only if it is healthy."""
        name, build = self.current()
        frm, to = st.get("from", ""), st.get("to", "")
        if not (same_build(build, to) and frm and frm != name):
            self.save(st, state="current" if name else "", current=build)
            return
        why = self.healthy(build)
        if not why:
            self.result(f"update {short(frm)} -> {short(to)}: interrupted, but {short(to)} runs fine")
            self.save(st, state="current", current=build)
            return
        back = self._switch_back(frm, name)
        if same_build(self.build_of(frm), to) and st.get("adapter_to"):     # an adapter-only rebuild (#301)
            self.adapter_failed(st, str(st["adapter_to"]), f"interrupted; then {why}")
        else:
            self._failed(st, to, f"interrupted; then {why}")
        self.result(f"update {short(frm)} -> {short(to)}: interrupted and unhealthy ({why}): back on {short(frm)}: "
                    + ("ok" if not back else f"FAILED: {back}"))
        self.save(st, state="failed", current=self.current()[1])

    def _switch_back(self, name: str, leaving: str) -> str:
        """Back to release NAME (a rename and a restart: nothing rebuilt): "" when it answers."""
        try:
            self.activate(name, leaving)
        except StepError as e:
            return str(e)[:300]
        return self.healthy(self.build_of(name) or name)

    def update(self, st: dict, name: str, build: str, want: str,
               adapter: tuple[dict | None, str] | None = None) -> int:
        """To the hub's build want, side by side. adapter: (settings, commit) for a rebuild of the running build
        with another incident-source adapter commit (or none: (None, "")); None: a sizeup update, which keeps the
        adapter set up (pick_adapter)."""
        t0 = self.clock()
        only = adapter is not None
        have = self.release_adapter(name)
        have_c = (have or {}).get("commit", "")
        if only:
            ad, ac = adapter
            tag = (f"adapter {repo_name((ad or have or {}).get('repo'))} {have_c[:7] or 'none'} -> {ac[:7] or 'none'}"
                   f" on {short(build)}")
            last = {"at": t0, "from": build, "to": build, "adapter": ac[:12], "adapter_from": have_c[:12]}
            self.save(st, state="updating", to=build, paused="", adapter_to=ac, **{"from": name})
            sha = build
        else:
            tag = f"update {short(build)} -> {short(want)}"
            last = {"at": t0, "from": build, "to": want}
            self.save(st, state="updating", to=want, hub=want, paused="", adapter_to="", **{"from": name})
            try:
                self.git("fetch", "--force", "origin", f"+refs/heads/{MAIN}:refs/remotes/origin/{MAIN}")
            except StepError as e:
                self.result(f"{tag}: can't fetch ({e}): staying on {short(build)}; trying again at the next check")
                self.save(st, state="behind", current=build, last=dict(last, result="no fetch", why=str(e)[:300]))
                return 1
            sha = self.git("rev-parse", "--verify", "--quiet", f"{want}^{{commit}}", check=False)
            on_main = bool(sha) and self.is_ancestor(sha, f"refs/remotes/origin/{MAIN}")
            if not on_main:
                why = f"not on origin/{MAIN}" if sha else "no such commit in the repository"
                self._failed(st, sha or want, why)
                self.result(f"{tag}: refused: {short(want)} is {why} (the station only runs what {MAIN} has)")
                self.save(st, state="failed", current=build, last=dict(last, result="refused", why=why))
                return 1
            last["to"] = sha
            ad, ac = self.pick_adapter(st, name)
            if ac or have_c:
                last.update(adapter=ac[:12], adapter_from=have_c[:12])
            self.save(st, to=sha, adapter_to=ac)
            if build and FULL_SHA.fullmatch(build) and ac == have_c and not runtime_changes(self.changed(build, sha)):
                eq = dict(st.get("equivalent") or {}) if isinstance(st.get("equivalent"), dict) else {}
                eq[sha] = build
                st["equivalent"] = dict(list(eq.items())[-KEEP_EQUIVALENT:])
                self.result(f"{tag}: nothing the station runs changed (docs, tests, CI, the hub's files): "
                            f"no restart; up to date")
                self.save(st, state="current", current=build, hub=want,
                          last=dict(last, result="no runtime change", why="", seconds=0))
                return 0
        n = 1 if only else self._attempt(st, sha)
        self.note(f"auto-update: {tag}: building the release beside the running one"
                  + ("" if only else f" (try {n} of {MAX_STAGES})"))
        step = "stage"
        try:
            self.stage(sha)
            step = "adapter"
            if ac:
                target = self.build_adapter(sha, ad, ac, st)
            else:
                self.clean_base(sha, st)
                target = sha
        except (StepError, OSError) as e:
            return self._not_built(st, e, step, tag, sha, build, ac, have_c, n, only, last)
        st.pop("retry", None)
        staged = self.clock() - t0
        self.save(st, state="waiting", to_release=target)
        waited, busy = wait_quiet(self.busy, self.quiet_max_s, self.clock, self.sleep)
        if busy:
            self.note(f"auto-update: still busy after {waited:.0f} s ({busy}): switching anyway")
        before = self.radios_ok()
        self.save(st, state="updating")
        t1 = self.clock()
        try:
            self.activate(target, name)
            why = self.healthy(sha)
        except StepError as e:
            why = str(e)[:300]
        up = self.clock() - t1
        if not why:
            radios = self.radios_back(before, t1)
            self.prune()
            win = self.windows()
            secs = self.clock() - t0
            gap = f"web back {up:.0f} s after the switch" + (f", radios {radios:.0f} s" if radios is not None else "")
            self.result(f"{tag}: ok in {secs:.0f} s (built {staged:.0f} s, waited {waited:.0f} s for a quiet moment, "
                        f"{gap})" + (f" · {win}" if win else ""))
            self.save(st, state="current", current=sha, windows=win, **({} if only else {"hub": want}),
                      last=dict(last, result="ok", why="", seconds=round(secs), staged_s=round(staged),
                                waited_s=round(waited), up_s=round(up, 1),
                                radios_s=None if radios is None else round(radios, 1)))
            return 0
        back = self._switch_back(name, target)
        if only:
            if ac:
                self.adapter_failed(st, ac, why)
            again = f"not trying adapter {ac[:7]} again until its ref moves" if ac else "trying again at the next check"
        else:
            self._failed(st, sha, why)
            again = f"not trying {short(sha)} again until the hub moves on"
        secs = self.clock() - t0
        self.result(f"{tag}: FAILED ({why}): back on {short(build)}" + (f" (adapter {have_c[:7]})" if have_c else "")
                    + f": {'ok' if not back else 'FAILED TOO: ' + back}; {again}")
        self.save(st, state="failed", current=self.current()[1] or build,
                  last=dict(last, result="rolled back" if not back else "rollback failed",
                            why=why + ("" if not back else f"; going back: {back}"), seconds=round(secs)))
        return 1

    def _not_built(self, st: dict, e: Exception, step: str, tag: str, sha: str, build: str, ac: str, have_c: str,
                   n: int, only: bool, last: dict) -> int:
        """The release (or its adapter) couldn't be built or failed its preflight: nothing switched."""
        secs = round(self.clock() - t0) if (t0 := last.get("at")) else 0
        preflight = isinstance(e, StepError) and e.code == PREFLIGHT_EXIT
        if step == "adapter" and preflight and ac != have_c:
            # a new adapter commit that doesn't work: not again until its ref moves; sizeup goes on without it
            self.adapter_failed(st, ac, str(e))
            st.pop("retry", None)
            self.result(f"{tag}: not switched (adapter {ac[:7]}: {e}); still on {short(build)}; adapter {ac[:7]} "
                        "not tried again until its ref moves")
            self.save(st, state="current" if only else "behind", current=build,
                      last=dict(last, result="adapter preflight failed", why=str(e)[:300], seconds=secs))
            return 1
        if only:
            self.result(f"{tag}: not built ({e}); still on {short(build)}; trying again at the next check")
            self.save(st, state="current", current=build,
                      last=dict(last, result="build failed, will retry", why=str(e)[:300], seconds=secs))
            return 1
        if preflight or n >= MAX_STAGES:
            why = (f"preflight: {e}" if preflight else f"couldn't build it {n} times: {e}")[:300]
            self._failed(st, sha, why)
            st.pop("retry", None)
            self.result(f"{tag}: not switched ({why}); still on {short(build)}; not trying {short(sha)} again "
                        "until the hub moves on")
            self.save(st, state="failed", current=build,
                      last=dict(last, result="preflight failed" if preflight else "build failed", why=why, seconds=secs))
            return 1
        st["retry"][sha]["why"] = str(e)[:200]
        at = _clock(st["retry"][sha]["next_at"])
        self.result(f"{tag}: couldn't build it yet ({e}); still on {short(build)}; trying again after {at} "
                    f"(try {n} of {MAX_STAGES})")
        self.save(st, state="behind", current=build,
                  last=dict(last, result="build failed, will retry", why=str(e)[:300], seconds=secs))
        return 1

    def changed(self, a: str, b: str) -> list[str]:
        """The paths that differ between two commits (unknown: one that isn't there = everything)."""
        try:
            return [f for f in self.git("diff", "--name-only", a, b).splitlines() if f.strip()]
        except StepError:
            return ["?"]

    def is_ancestor(self, sha: str, ref: str) -> bool:
        try:
            self.git("merge-base", "--is-ancestor", sha, ref)
            return True
        except StepError:
            return False

    def prune(self) -> list[str]:
        """Keep KEEP_RELEASES releases (the current one and the last ones switched to); remove the
        rest, the builds none of the kept ones use, and leftovers of an interrupted stage."""
        name = self.current()[0]
        if not name:
            return []
        gone = prune_plan(self.releases(), self.history(), name)
        for r in gone:
            shutil.rmtree(self.p.releases / r, ignore_errors=True)
        for d in self.p.releases.glob(".staging-*"):
            shutil.rmtree(d, ignore_errors=True)
        used = set()
        for r in self.releases():
            for line in (self.p.releases / r / "BUILDS").read_text().splitlines() \
                    if (self.p.releases / r / "BUILDS").is_file() else []:
                used.add(os.path.realpath(line.partition("=")[2].strip()))
        for link in ("rtl-sdr", "trunk-recorder", "whisper"):
            used.add(os.path.realpath(self.p.opt / link))
        if self.p.builds.is_dir():
            for d in self.p.builds.iterdir():
                if d.is_dir() and os.path.realpath(d) not in used:
                    shutil.rmtree(d, ignore_errors=True)
                    gone.append(f"builds/{d.name}")
        legacy = (self.p.releases / ".legacy")
        if legacy.is_file() and legacy.read_text().strip() not in self.releases():
            # the release made from the checkout that ran before releases is gone: so can that be
            for old in (self.p.opt / "app", self.p.opt / "venv"):
                if old.is_dir() and not old.is_symlink():
                    shutil.rmtree(old, ignore_errors=True)
                    gone.append(old.name)
            legacy.unlink(missing_ok=True)
        hist = [h for h in self.history() if h in self.releases()]
        try:
            (self.p.releases / ".history").write_text("".join(f"{h}\n" for h in hist[-50:]))
        except OSError:
            pass
        if gone:
            self.note(f"auto-update: pruned {', '.join(gone)}")
        return gone

    def rollback(self) -> int:
        """sizeup-station rollback: back to the release that ran before; the one left is not tried
        again until the hub moves on."""
        st = self.load()
        name, build = self.current()
        prev = self.previous()
        if not prev:
            print("No earlier release to go back to.", file=sys.stderr)
            return 1
        why = self._switch_back(prev, name)
        new = self.current()[1]
        left = (self.release_adapter(name) or {}).get("commit", "")
        if same_build(build, self.build_of(prev)) and left:
            # the same sizeup, another adapter: that adapter commit is the one left (#301)
            self.adapter_failed(st, left, "rolled back by hand (sizeup-station rollback)")
        else:
            self._failed(st, build, "rolled back by hand (sizeup-station rollback)")
        self.result(f"rollback {short(build)} -> {short(new)}: " + ("ok" if not why else f"FAILED: {why}"))
        self.save(st, state="failed", current=new,
                  last={"at": self.clock(), "from": build, "to": new, "result": "rolled back by hand", "why": why})
        return 0 if not why else 1

    def windows(self) -> str:
        """WSL: C:\\ProgramData\\sizeup\\*.ps1 like the current release's deploy/station/windows/ (copied
        over when they differ), else WINDOWS_STALE. "" when they match or this isn't WSL."""
        if not self.is_wsl() or not self.p.windows.is_dir():
            return ""
        stale = []
        for src in sorted((self.p.current / "deploy" / "station" / "windows").glob("*.ps1")):
            dst = self.p.windows / src.name
            if same_text(src, dst):
                continue
            tmp = dst.with_name(f".{dst.name}.tmp")
            try:
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)               # (a task starting now sees the old one or the new one)
                self.note(f"auto-update: refreshed {dst}")
                continue
            except OSError as e:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                self.note(f"auto-update: can't write {dst}: {e}")
            stale.append(src.name)
        return f"{WINDOWS_STALE} ({', '.join(stale)})" if stale else ""

    # -- what people read (sizeup-station status / doctor) ---------------------------------------------------

    def report(self) -> dict:
        st = self.load()
        for k in ("last", "failed", "retry", "equivalent"):          # (one shape, whatever an older run wrote)
            if k in st and not isinstance(st[k], dict):
                st[k] = {}
        name, build = self.current()
        want = self.read_want()
        paused = paused_reason(self.settings())
        d = describe(build, want, dict(st, paused=paused))
        win = st.get("windows", "") if os.geteuid() != 0 else self.windows()
        installed = self.release_adapter(name)
        ad = self.adapter()
        rec = dict(st.get("adapter") or {}) if isinstance(st.get("adapter"), dict) else {}
        if ad:
            rec.setdefault("configured", True)
            rec.setdefault("repo", repo_name(ad.get("repo")))
            if ad.get("why"):
                rec["error"] = ad["why"]
        adapter = dict(adapter_view(installed, rec), ref=(ad or installed or {}).get("ref", ""),
                       env=[(k, self.env_has(k)) for k in (ad or {}).get("env") or []],
                       key=self.p.adapter_key.is_file(), sources=(installed or {}).get("sources") or [],
                       key_open=self.key_open())
        return {"build": build, "release": name, "hub": want, "paused": paused, "record": st, "windows": win,
                "releases": self.releases(), "previous": self.previous(), "adapter": adapter, **d}


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _when(t) -> str:
    return _clock(t, "%Y-%m-%d %H:%M")


def status_lines(r: dict) -> list[str]:
    last = _dict(_dict(r.get("record")).get("last"))
    out = [f"version:   {short(r['build'])} (release {r['release'] or 'none yet: sudo sizeup-station update'})",
           f"hub:       {short(r['hub']) if r['hub'] else 'not known yet (the station passes it on once the hub answers)'}"
           f" · {r['text']}",
           f"auto-update: {'off: ' + r['paused'] if r['paused'] else 'on (follows the hub build)'}",
           f"releases:  {len(r['releases'])} kept" + (f", rollback goes to {short(r['previous'])}" if r["previous"] else "")]
    if last:
        out.append(f"last update: {_when(last.get('at'))} {short(last.get('from'))} -> {short(last.get('to'))}: "
                   f"{last.get('result', '?')}" + (f" ({last['why']})" if last.get("why") else "")
                   + (f" in {last['seconds']} s" if last.get("seconds") is not None else "")
                   + (f"; web back after {last['up_s']} s" if last.get("up_s") is not None else "")
                   + (f", radios after {last['radios_s']} s" if last.get("radios_s") is not None else ""))
    if r.get("windows"):
        out.append(f"windows:   {r['windows']}")
    ad = _dict(r.get("adapter"))
    if ad.get("state", "off") != "off":
        out.append(f"adapter:   {ad.get('text')}" + (f" (ref {ad['ref']})" if ad.get("ref") else "")
                   + (f"; sources {', '.join(ad['sources'])}" if ad.get("sources") else ""))
    return out


def doctor_lines(r: dict) -> list[str]:
    out = [f"  note  auto-update off: {r['paused']}" if r["paused"] else "  ok    auto-update on: follows the hub's build"]
    if not r["release"]:
        out.append("  FAIL  not on side-by-side releases yet: sudo sizeup-station update")
    if r["state"] == "ok":
        out.append(f"  ok    version {short(r['build'])}: the hub's")
    elif r["state"] == "failed":
        out.append(f"  FAIL  {r['text']} (journalctl -u sizeup-update; /var/log/sizeup-update.log)")
    else:
        out.append(f"  note  version: {r['text']}")
    if r["windows"]:
        out.append(f"  FAIL  {r['windows']}")
    ad = _dict(r.get("adapter"))
    if ad.get("state", "off") != "off":
        if ad["state"] == "loaded":
            out.append(f"  ok    adapter {ad.get('text')}")
        elif ad["state"] == "failed":
            out.append(f"  FAIL  adapter {ad.get('text')} (journalctl -u sizeup-update)")
        else:
            out.append(f"  note  adapter {ad.get('text')} (sudo sizeup-station autoupdate now)")
        if not ad.get("key"):
            out.append("  FAIL  adapter: no deploy key: sudo sizeup-station adapter setup")
        elif ad.get("key_open"):
            out.append(f"  FAIL  adapter: {ad['key_open']}: sudo chmod 600 /etc/sizeup/adapter-deploy-key")
        for k, has in ad.get("env") or []:
            out.append(f"  ok    {k} is set" if has else f"  note  {k} is not set: sudo sizeup-station adapter setup"
                       if has is False else f"  note  {k}: station.env not readable here")
    return out


# -- sizeup-station adapter setup | status | remove (#301) -----------------------------------------------------

def keygen(key: Path) -> None:
    """A new ed25519 key at key (root 0600) and key.pub."""
    host = os.uname().nodename.split(".")[0] or "station"
    key.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"sizeup-adapter@{host}", "-f", str(key)],
                   check=True, capture_output=True, timeout=60)


def public_key(key: Path) -> str:
    try:
        pub = key.with_name(key.name + ".pub").read_text().strip()
    except OSError:
        pub = subprocess.run(["ssh-keygen", "-y", "-f", str(key)], capture_output=True, text=True, timeout=30).stdout.strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9@._-]+)?", pub):
        raise StepError(f"{key}.pub isn't an ed25519 public key")
    return pub


def adapter_setup(u: Updater, repo: str = "", ref: str = "", env: str | None = None, check: bool = True,
                  ask: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass,
                  out: Callable[[str], None] = print, make_key: Callable[[Path], None] = keygen,
                  reach: Callable[[dict], str] | None = None) -> int:
    """sudo sizeup-station adapter setup: the deploy key (only its public half is printed), the repo and ref, the
    credentials (hidden, into station.env), [adapter] in the config (now, or at the next update). 0 = saved."""
    s = u.settings()
    if str(s.get("role") or "").strip().lower() == "hub":
        out(f"Refused: {HUB_REFUSED} ([cloud] role = \"hub\" in {u.p.config}).")
        return 2
    cur = u.adapter() or {}

    def pick(given: str, prompt: str, default: str, problem: Callable[[str], str]) -> str | None:
        for _ in range(3):
            v = given or ask(prompt + (f" [{default}]" if default else "") + ": ").strip() or default
            why = problem(v) if v else "required"
            if not why:
                return v
            out(f"  {why}")
            if given:
                return None
        return None

    repo = pick(repo, "Adapter repo (SSH URL)", cur.get("repo", ""), adapter_repo_problem)
    if repo is None:
        return 2
    ref = pick(ref, "Branch or tag", cur.get("ref") or MAIN,
               lambda v: "" if ADAPTER_REF.fullmatch(v) else "a branch or tag name")
    if ref is None:
        return 2
    if env is None:
        env = ask(f"Credential variable names, comma-separated [{','.join(cur.get('env') or [])}]: ").strip() \
            or ",".join(cur.get("env") or [])
    names = list(dict.fromkeys(n.strip() for n in env.split(",") if n.strip()))
    bad = [p for p in map(env_problem, names) if p]
    if bad:
        out("  " + "; ".join(bad))
        return 2
    key = u.p.adapter_key
    if not key.is_file():
        make_key(key)
    os.chmod(key, 0o600)
    if os.geteuid() == 0:
        os.chown(key, 0, 0)
    pub = public_key(key)
    slug = github_slug(repo)
    host = os.uname().nodename.split(".")[0] or "station"
    out("")
    out("Deploy key (public, read-only). On your Mac:")
    if slug:
        out(f"  echo '{pub}' | gh repo deploy-key add - -R {slug} --title 'sizeup station {host}'")
    else:
        out(f"  {pub}")
        out("  (add it to the repo as a read-only deploy key)")
    out("")
    values = {}
    for k in names:
        has = u.env_has(k)
        for _ in range(3):
            v = secret(f"{k} (hidden" + ("; Enter keeps it" if has else "") + "): ")
            if not v:
                if not has:
                    out(f"  {k}: not set")
                break
            if any(c in v for c in "\n\r\0"):
                out("  one line, please")
                continue
            values[k] = v
            break
    old = set(cur.get("env") or []) - set(names)
    if values or old:
        u.write_env(values, drop=old)
    values.clear()
    u.p.adapter_pending.write_text(json.dumps({"repo": repo, "ref": ref, "env": names}, indent=1) + "\n")
    u.p.adapter_pending.chmod(0o644)
    rc, said = u.configupdate()
    for line in said.splitlines():
        if line.startswith("adapter"):
            out(f"config: {line}")
    if rc != 0:
        out(f"config: not changed ({said.splitlines()[-1] if said else '?'}); the updater uses the saved setup")
    u._settings = None
    if check:
        reach = reach or (lambda a: u.adapter_reach(a))
        a = u.adapter()
        while True:
            why = reach(a)
            if not why:
                out(f"Access: ok ({repo_name(repo)} {ref})")
                break
            out(f"Access: {why}")
            if ask("Press Enter once the key is added on GitHub (s to skip): ").strip().lower() == "s":
                break
    out("Saved. Installed at the next update check (sudo sizeup-station autoupdate now).")
    return 0


def adapter_remove(u: Updater, yes: bool = False, ask: Callable[[str], str] = input,
                   out: Callable[[str], None] = print) -> int:
    """sudo sizeup-station adapter remove: the key, its credentials, [adapter]; the next update runs without it."""
    cur = u.adapter() or {}
    if not cur and not u.release_adapter(u.current()[0]):
        out("No adapter set up.")
        return 0
    if not yes and ask(f"Remove the adapter {repo_name(cur.get('repo'))} (key, credentials, config)? [y/N] ").strip().lower() != "y":
        out("Nothing changed.")
        return 1
    names = set(cur.get("env") or [])
    if names:
        u.write_env({}, drop=names)
    for f in (u.p.adapter_key, u.p.adapter_key.with_name(u.p.adapter_key.name + ".pub"), u.p.adapter_pending):
        f.unlink(missing_ok=True)
    rc, said = u.configupdate("--remove-adapter")
    for line in said.splitlines():
        out(f"config: {line}")
    shutil.rmtree(u.p.adapter_repo, ignore_errors=True)
    st = u.load()
    rec = u.adapter_rec(st)
    u.save(st, adapter={"dists": rec.get("dists") or [], "configured": False, "failed": {}})
    slug = github_slug(cur.get("repo"))
    out("Removed. The next update check switches to a release without it (sudo sizeup-station autoupdate now).")
    if slug:
        out(f"On your Mac: gh repo deploy-key list -R {slug}, then gh repo deploy-key delete <id> -R {slug}")
    return 0 if rc == 0 else 1


def adapter_cli(u: Updater, a) -> int:
    sub = a.sub[0] if a.sub else "status"
    if sub == "setup":
        return u.locked(lambda: adapter_setup(u, a.repo, a.ref, a.env, check=not a.no_check and sys.stdin.isatty()))
    if sub == "remove":
        return u.locked(lambda: adapter_remove(u, yes=a.yes))
    if sub == "stage" and len(a.sub) == 2:
        # install-linux.sh (an update by hand): release NAME with the adapter set up; prints the release to switch to
        def stage() -> int:
            st = u.load()
            ad, commit = u.pick_adapter(st, u.current()[0])
            name = a.sub[1]
            try:
                if commit:
                    name = u.build_adapter(name, ad, commit, st)
                else:
                    u.clean_base(name, st)
            except (StepError, OSError) as e:
                print(f"adapter: {e}", file=sys.stderr)
                if commit and isinstance(e, StepError) and e.code == PREFLIGHT_EXIT:
                    u.adapter_failed(st, commit, str(e))
                u.save(st)
                return 1
            u.save(st)
            print(name)
            return 0
        return u.locked(stage)
    if sub == "status":
        r = u.report()
        ad = _dict(r.get("adapter"))
        print(f"adapter:   {ad.get('text') or 'none (sudo sizeup-station adapter setup)'}"
              + (f" (ref {ad['ref']})" if ad.get("ref") else ""))
        if ad.get("state", "off") != "off":
            print(f"key:       {'yes' if ad.get('key') else 'no'} ({u.p.adapter_key})")
            for k, has in ad.get("env") or []:
                print(f"{k}: {'set' if has else 'not set' if has is False else '?'}")
        return 0
    print("sizeup-station adapter setup | status | remove", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autoupdate.py", description="the station follows the hub's build")
    ap.add_argument("cmd", choices=["run", "status", "doctor", "on", "off", "prune", "rollback", "adapter"])
    ap.add_argument("sub", nargs="*", help="adapter: setup | status | remove | stage RELEASE")
    ap.add_argument("--quiet-max", type=float, default=QUIET_MAX_S, help="seconds to wait for a quiet moment")
    ap.add_argument("--health-timeout", type=float, default=HEALTH_S, help="seconds for /healthz after the switch")
    ap.add_argument("--repo", default="", help="adapter setup: the adapter repo (SSH URL)")
    ap.add_argument("--ref", default="", help="adapter setup: its branch or tag (default main)")
    ap.add_argument("--env", default=None, help="adapter setup: its credential variable names, comma-separated")
    ap.add_argument("--no-check", action="store_true", help="adapter setup: don't wait for the repo to answer")
    ap.add_argument("--yes", action="store_true", help="adapter remove: don't ask")
    a = ap.parse_args(argv)
    u = Updater(quiet_max_s=a.quiet_max, health_s=a.health_timeout)
    if a.cmd == "adapter":
        return adapter_cli(u, a)
    if a.sub:
        ap.error(f"unexpected {' '.join(a.sub)}")
    if a.cmd == "run":
        return u.run()
    if a.cmd == "prune":
        def prune() -> int:
            u.prune()
            return 0
        return u.locked(prune)
    if a.cmd == "rollback":
        return u.locked(u.rollback)
    if a.cmd in ("on", "off"):
        s = u.settings()
        if a.cmd == "on" and s["ref"] != MAIN:
            print(f"This station runs --ref {s['ref']}. Auto-update follows the hub, which runs {MAIN}: "
                  f"sizeup-station update --ref {MAIN} first (that turns it back on).", file=sys.stderr)
            return 2
        set_kv(u.p.install_conf, "SIZEUP_AUTO_UPDATE", a.cmd)
        u._settings = None
        why = paused_reason(u.settings())
        print(f"auto-update {a.cmd}" + (f" (but still off: {why})" if a.cmd == "on" and why else ""))
        return 0
    try:
        r = u.report()
        print("\n".join(status_lines(r) if a.cmd == "status" else doctor_lines(r)))
    except Exception as e:  # noqa: BLE001 - status must never take the CLI down
        print(f"version:   ? (the updater's state can't be read: {type(e).__name__}: {e})" if a.cmd == "status"
              else f"  FAIL  the updater's state can't be read ({type(e).__name__}: {e}): /var/lib/sizeup-update/state.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
