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
                  are swapped by rename, one restart of sizeup-station (whisper-server is its own
                  service and keeps its model loaded)
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

    python3 autoupdate.py run | status | doctor | on | off | prune | rollback
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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
NOT_RADIOS = ("Hub", "STT")
WINDOWS_STALE = "Windows scripts out of date: run install-windows.ps1"


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


def plan(want: str, current: str, paused: str, failed: dict | None) -> tuple[str, str]:
    """What to do about the hub's build: (action, why). Actions: "none" (the hub's build isn't
    known), "current", "paused", "failed-before", "update"."""
    if not norm(want):
        return "none", "the hub's build isn't known"
    if same_build(want, current):
        return "current", f"on the hub's build {short(want)}"
    if paused:
        return "paused", paused
    f = failed_entry(failed, want)
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
    if (upd.get("state") in ("waiting", "updating") and same_build(upd.get("to"), hub)
            and now - float(upd.get("since") or 0) < STALE_ATTEMPT_S):
        wait = " (waiting for a quiet moment)" if upd.get("state") == "waiting" else ""
        return {"state": "updating", "text": f"updating {short(build)} -> {short(hub)}{wait}{win}"}
    f = failed_entry(upd.get("failed"), hub)
    if f is not None:
        return {"state": "failed", "text": f"update to {short(hub)} failed ({f.get('why') or '?'}): "
                                           f"staying on {short(build)} until the hub moves on{win}"}
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
                 log: Callable[[str], None] = print):
        self.own = norm(os.environ.get("SIZEUP_BUILD", "") if own is None else own)
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
        out = {k: st[k] for k in ("state", "since", "to", "from", "paused", "windows", "last") if k in st}
        out["failed"] = _newest_failed(st.get("failed") if isinstance(st.get("failed"), dict) else {}, 5)
        return out

    def status(self) -> dict:
        """{build, hub, update, state, text}: in the station's status to the hub, and its Hub row."""
        upd = self.state()
        return dict({"build": self.own, "hub": self.hub_build, "update": upd},
                    **describe(self.own, self.hub_build, upd, self.clock()))


# -- the updater (root) --------------------------------------------------------------------------------

class StepError(RuntimeError):
    pass


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
        proc = subprocess.Popen(["bash", str(script), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            self.out(line)
        if proc.wait() != 0:
            said = [ln.split("  ", 1)[-1] for ln in tail if "FAILED" in ln or "WARNING" in ln]
            raise StepError("; ".join(said[-3:]) or f"{script.name} {args[0]} failed (exit {proc.returncode})")

    def stage(self, sha: str) -> None:
        """That commit's own installer builds and preflights releases/<sha> (nothing switches)."""
        self.p.work.mkdir(parents=True, exist_ok=True)
        script = self.p.work / "install-linux.sh"
        script.write_text(self.git("show", f"{sha}:deploy/station/install-linux.sh") + "\n")
        script.chmod(0o700)
        self.installer(["--stage", sha], script)

    def activate(self, name: str, leaving: str = "") -> None:
        """install-linux.sh --activate NAME: links swapped, one restart. The release's own installer;
        one from before side-by-side releases has none that can, so the one we leave does it."""
        for cand in (name, leaving, *reversed(self.history())):
            script = self.p.releases / cand / "deploy/station/install-linux.sh" if cand else None
            if script is not None and script.is_file() and "--activate)" in script.read_text(errors="replace"):
                self.installer(["--activate", name], script)
                return
        raise StepError(f"no installer that can switch to {name}")

    def _get(self, path: str, timeout: float = 5.0) -> dict | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.settings()['port']}{path}", timeout=timeout) as r:
                d = json.loads(r.read() or b"{}")
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
            self._settings = {"ref": kv.get("SIZEUP_REF", MAIN), "source": kv.get("SIZEUP_SOURCE", "git"),
                              "switch": kv.get("SIZEUP_AUTO_UPDATE", "on"),
                              "config": station.get("auto_update", True) if isinstance(station, dict) else True,
                              "port": int(web.get("port", 8091)) if isinstance(web, dict) else 8091}
        return self._settings

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
        failed = self.load().get("failed") or {}
        for name in reversed(self.history()):
            if name != cur and name in have and failed_entry(failed, self.build_of(name)) is None:
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
        """The one line per attempt: the journal and /var/log/sizeup-update.log."""
        self.out(line)
        try:
            with self.p.log.open("a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.clock()))}  {line}\n")
        except OSError:
            pass

    def read_want(self) -> str:
        """The hub's build as the station last heard it; "" if none, malformed or too old."""
        try:
            age = time.time() - self.p.want.stat().st_mtime
            words = self.p.want.read_text().split()
        except OSError:
            return ""
        if age > WANT_MAX_AGE_S or not words:
            return ""
        return norm(words[0])

    def _failed(self, st: dict, sha: str, why: str) -> None:
        failed = st.get("failed") if isinstance(st.get("failed"), dict) else {}
        failed[sha] = {"at": self.clock(), "why": why[:300]}
        st["failed"] = _newest_failed(failed, KEEP_FAILED)

    # -- the work -----------------------------------------------------------------------------------------------

    def locked(self, fn: Callable[[], int]) -> int:
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
        action, why = plan(want, build, paused, st.get("failed"))
        if action == "none":
            return 0
        if action == "current":
            if st.get("state") != "current" or st.get("current") != build or st.get("paused") != paused:
                self.save(st, state="current", current=build, hub=want, paused=paused)
            return 0
        if action in ("paused", "failed-before"):
            if st.get("state") != action or not same_build(st.get("hub"), want) or st.get("paused") != paused:
                self.save(st, state=action, current=build, hub=want, paused=paused)
                self.note(f"auto-update: not updating to {short(want)}: {why}")
            return 0
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

    def update(self, st: dict, name: str, build: str, want: str) -> int:
        t0 = self.clock()
        tag = f"update {short(build)} -> {short(want)}"
        last = {"at": t0, "from": build, "to": want}
        self.save(st, state="updating", to=want, hub=want, paused="", **{"from": name})
        try:
            self.git("fetch", "--prune", "--tags", "--force", "origin")
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
        self.save(st, to=sha)
        self.note(f"auto-update: {tag}: building the release beside the running one")
        try:
            self.stage(sha)
        except (StepError, OSError) as e:
            why = f"preflight: {e}"[:300]
            self._failed(st, sha, why)
            secs = self.clock() - t0
            self.result(f"{tag}: not switched ({why}); still on {short(build)}; not trying {short(sha)} again "
                        "until the hub moves on")
            self.save(st, state="failed", current=build,
                      last=dict(last, result="preflight failed", why=why, seconds=round(secs)))
            return 1
        staged = self.clock() - t0
        self.save(st, state="waiting")
        waited, busy = wait_quiet(self.busy, self.quiet_max_s, self.clock, self.sleep)
        if busy:
            self.note(f"auto-update: still busy after {waited:.0f} s ({busy}): switching anyway")
        before = self.radios_ok()
        self.save(st, state="updating")
        t1 = self.clock()
        try:
            self.activate(sha, name)
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
            self.save(st, state="current", current=sha, hub=want, windows=win,
                      last=dict(last, result="ok", why="", seconds=round(secs), staged_s=round(staged),
                                waited_s=round(waited), up_s=round(up, 1),
                                radios_s=None if radios is None else round(radios, 1)))
            return 0
        back = self._switch_back(name, sha)
        self._failed(st, sha, why)
        secs = self.clock() - t0
        self.result(f"{tag}: FAILED ({why}): back on {short(build)}: {'ok' if not back else 'FAILED TOO: ' + back}; "
                    f"not trying {short(sha)} again until the hub moves on")
        self.save(st, state="failed", current=self.current()[1] or build,
                  last=dict(last, result="rolled back" if not back else "rollback failed",
                            why=why + ("" if not back else f"; going back: {back}"), seconds=round(secs)))
        return 1

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
            try:
                shutil.copyfile(src, dst)
                self.note(f"auto-update: refreshed {dst}")
                continue
            except OSError as e:
                self.note(f"auto-update: can't write {dst}: {e}")
            stale.append(src.name)
        return f"{WINDOWS_STALE} ({', '.join(stale)})" if stale else ""

    # -- what people read (sizeup-station status / doctor) ---------------------------------------------------

    def report(self) -> dict:
        st = self.load()
        name, build = self.current()
        want = self.read_want()
        paused = paused_reason(self.settings())
        d = describe(build, want, dict(st, paused=paused))
        win = st.get("windows", "") if os.geteuid() != 0 else self.windows()
        return {"build": build, "release": name, "hub": want, "paused": paused, "state": st, "windows": win,
                "releases": self.releases(), "previous": self.previous(), **d}


def _when(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t))) if t else "?"


def status_lines(r: dict) -> list[str]:
    last = (r["state"] or {}).get("last") or {}
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
    if r["windows"]:
        out.append(f"windows:   {r['windows']}")
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
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autoupdate.py", description="the station follows the hub's build")
    ap.add_argument("cmd", choices=["run", "status", "doctor", "on", "off", "prune", "rollback"])
    ap.add_argument("--quiet-max", type=float, default=QUIET_MAX_S, help="seconds to wait for a quiet moment")
    ap.add_argument("--health-timeout", type=float, default=HEALTH_S, help="seconds for /healthz after the switch")
    a = ap.parse_args(argv)
    u = Updater(quiet_max_s=a.quiet_max, health_s=a.health_timeout)
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
    r = u.report()
    print("\n".join(status_lines(r) if a.cmd == "status" else doctor_lines(r)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
