"""Give one RTL-SDR a new USB serial (GitHub #242): `sizeup-station sdr-serial <index|current-serial> <new-serial>`.

Two dongles with one serial can't be told apart (a Nooelec NESDR SMArt plugged in with its factory 00000001, the
station's "V3 #2" serial too, took Fire/EMS 15 off the hub on 2026-11-04): sizeup then opens neither by that serial
and raises a critical alert whose text names this command. What it does, in order, stopping at the first problem:

    1. checks the new serial: 3-16 characters of A-Z a-z 0-9 -, at least one letter (the rtl tools read a number as a
       device index), not the serial of a dongle plugged in now
    2. stops sizeup-station (and with it rtl_fm, its child), sizeup-trunk-recorder (#71), sizeup-adsb (readsb), and holds off
       the updater and the health timer, so nothing opens a dongle meanwhile
    3. if another app's claim (~/.local/state/rtl-claims: shortfinal) holds that serial, asks you to switch it Off
       there and waits: shortfinal is never stopped or killed from here
    4. backs up the dongle's EEPROM: rtl_eeprom -d N -r ~/sizeup-eeprom-backups/<old>-<date>.bin (the person's home
       under sudo); nothing is written unless that backup is there and is an RTL2832 EEPROM
    5. writes the new serial: rtl_eeprom -d N -s <new>, answering its "Write new configuration to device [y/n]?"
    6. asks you to unplug that dongle and plug it back in, waits, looks again: the new serial must be there, once
    7. the references to the old serial (the job saved for it in sdr_modes.json, the ADS-B choice, [radio] sdr_names /
       sdrs / sdr_ignore / a channel's device / [aircraft.receiver] sdr in config.toml, a trunk-recorder source): moved
       to the new serial in sizeup's own state files only when it is certain the renamed dongle is the one they meant
       (its serial was its own, or it has the product that serial had before a second dongle turned up with it,
       sdr_products.json); else left alone, and what to set is printed. config.toml is never edited: the lines to
       change are printed
    8. starts what it stopped and prints which job runs on which dongle now

    --dry-run   the checks and the plan, nothing stopped or written
    --yes       don't ask before writing (shortfinal's claim is still waited for)

Run as root (sizeup-station sdr-serial does). Never run by the tests against hardware: they hand in fake dongles and
a fake rtl_eeprom (tests/test_sdr_serial.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from sizeup.radio import adsbd, sdr

SERIAL_RE = re.compile(r"[A-Za-z0-9-]{3,16}")
SERVICES = ("sizeup-station.service", "sizeup-trunk-recorder.service", "sizeup-adsb.service")   # rtl_fm: sizeup-station's
HOLD = ("sizeup-update.path", "sizeup-update.timer", "sizeup-health.timer")
CHILDREN = "trunk-recorder|rtl_fm|readsb"
CONFIG = "/etc/sizeup/config.toml"
STATE = "/var/lib/sizeup"
RTL_FM = "/opt/sizeup/rtl-sdr/bin/rtl_fm"
RTL_EEPROM = "/opt/sizeup/rtl-sdr/bin/rtl_eeprom"
TR_CONFIG = "/var/lib/sizeup/trunk-recorder/config.json"
EEPROM_MIN = 256                     # rtl_eeprom -r writes the whole 256-byte EEPROM
REPLUG_S = 300.0
POLL_S = 2.0


def _home(user: str) -> Path | None:
    try:
        import pwd
        return Path(pwd.getpwnam(user).pw_dir)
    except (ImportError, KeyError):
        return None


@dataclass
class Env:
    """Everything that touches the machine, so the tests can hand in fakes."""
    devices: Callable[[], list[tuple[int, str, str]]] = lambda: sdr.devices(RTL_FM, fresh=True)
    run: Callable[..., subprocess.CompletedProcess] = lambda argv, **kw: subprocess.run(
        argv, capture_output=True, text=True, timeout=kw.pop("timeout", 60), **kw)
    ask: Callable[[str], str] = input
    say: Callable[[str], None] = lambda m: print(m, flush=True)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    is_root: Callable[[], bool] = lambda: os.geteuid() == 0
    rtl_eeprom: str = RTL_EEPROM
    config: str = CONFIG
    state: str = STATE
    tr_config: str = TR_CONFIG
    backups: Path | None = None
    claims_dirs: list[Path] = field(default_factory=list)
    systemd: bool = True
    api: Callable[[str], dict | None] | None = None

    def __post_init__(self):
        if self.backups is None:
            who = os.environ.get("SUDO_USER", "")
            self.backups = ((_home(who) if who and who != "root" else None) or Path.home()) / "sizeup-eeprom-backups"
        if not self.claims_dirs:
            homes = [Path(STATE), Path.home()] + ([h] if (h := _home(os.environ.get("SUDO_USER", "") or "-")) else [])
            self.claims_dirs = list(dict.fromkeys(h / ".local" / "state" / "rtl-claims" for h in homes))
        if self.api is None:
            self.api = self._api

    def _api(self, path: str) -> dict | None:
        try:
            port = int(tomllib.loads(Path(self.config).read_text()).get("web", {}).get("port", 8090))
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            port = 8090
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                return json.loads(r.read())
        except (OSError, ValueError):
            return None

    # -- sizeup's files --------------------------------------------------------------------------------------------

    def conf(self) -> dict:
        try:
            return tomllib.loads(Path(self.config).read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    def modes_file(self) -> Path:
        """sdr_modes.json: next to [storage] clips_dir (relative to the state dir), as sizeup/app.py keeps it."""
        clips = Path(str(self.conf().get("storage", {}).get("clips_dir", "captures/clips")))
        return (clips if clips.is_absolute() else Path(self.state) / clips).parent / "sdr_modes.json"

    def products_file(self) -> Path:
        return self.modes_file().with_name("sdr_products.json")

    def adsb_file(self) -> Path:
        db = Path(str(self.conf().get("cad", {}).get("db", "state/cad.sqlite")))
        return (db if db.is_absolute() else Path(self.state) / db).parent / Path(adsbd.OVERRIDE).name


def _read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _write_json(p: Path, d: dict) -> None:
    tmp = p.with_name(f".{p.name}.sdr-serial")
    tmp.write_text(json.dumps(d))
    try:
        st = p.stat()
        os.chown(tmp, st.st_uid, st.st_gid)        # still the sizeup user's
        os.chmod(tmp, st.st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, p)


def listing(devs: list[tuple[int, str, str]]) -> str:
    dups = sdr.duplicates(devs)
    return "\n".join(f"  #{i}  {s or '(no serial)'}  {p}" + ("   <- shared serial" if s in dups else "")
                     for i, p, s in devs)


def pick(target: str, devs: list[tuple[int, str, str]],
         products: dict | None = None) -> tuple[tuple[int, str, str] | None, str]:
    """The dongle `target` names: its exact serial, else its index. (None, why) when it names none, or two."""
    hits = [d for d in devs if d[2] == target]
    if len(hits) > 1:
        return None, (f"serial {target} is on {len(hits)} dongles ("
                      + ", ".join(f"#{i} {p}" for i, p, _s in hits) + "): say which by its index, e.g. "
                      f"sizeup-station sdr-serial {sdr.rename_index(target, [(i, p) for i, p, _s in hits], products)} <new-serial>")
    if hits:
        if target.isdigit() and any(i == int(target) and (i, p, s) != hits[0] for i, p, s in devs):
            return None, f"{target} is the serial of #{hits[0][0]} and the index of another dongle: say which by its index"
        return hits[0], ""
    if target.isdigit() and len(target) <= 3:
        d = next((d for d in devs if d[0] == int(target)), None)
        if d:
            return d, ""
    return None, f"no dongle plugged in has the index or serial {target!r}"


def serial_problem(new: str, devs: list[tuple[int, str, str]], old: str = "") -> str:
    """"" for a serial a dongle may be given, else why not."""
    if not SERIAL_RE.fullmatch(new or ""):
        return "a serial is 3 to 16 characters: letters, digits and - (e.g. hpn-twr, adsb1090)"
    if not any(c.isalpha() for c in new):
        return "a serial needs a letter: the rtl tools read a number as a device index (e.g. hpn-twr, adsb1090)"
    if new == old:
        return f"the dongle is {old} already"
    taken = [i for i, _p, s in devs if s.lower() == new.lower()]
    if taken:
        return f"{new} is already the serial of #{taken[0]}: pick another"
    return ""


# -- the references to a serial -------------------------------------------------------------------------------------


@dataclass
class Ref:
    where: str          # where it is ("sdr_modes.json", "config.toml [radio] sdr_names", ...)
    what: str           # what it says
    fix: str            # what to set for the new serial
    state: str = ""     # "modes" | "adsb": one of sizeup's own state files, updated by this tool when certain


def references(env: Env, old: str, new: str) -> list[Ref]:
    out: list[Ref] = []
    modes = _read_json(env.modes_file())
    if old in modes and isinstance(modes.get(old), str):
        out.append(Ref(str(env.modes_file()), f"{old} runs {modes[old]}", f"the job {modes[old]} under {new}", "modes"))
    ad = _read_json(env.adsb_file())
    if ad.get("sdr") == old:
        out.append(Ref(str(env.adsb_file()), f"the ADS-B receiver is {old}", f"the ADS-B receiver {new}", "adsb"))
    c = env.conf()
    radio = c.get("radio", {}) if isinstance(c.get("radio"), dict) else {}
    names = radio.get("sdr_names", {}) if isinstance(radio.get("sdr_names"), dict) else {}
    if old in names:
        out.append(Ref(f"{env.config} [radio] sdr_names", f'"{old}" = "{names[old]}"',
                       f'"{new}" = "{names[old]}"   (in place of "{old}" = "{names[old]}")'))
    for key in ("sdrs", "sdr_ignore"):
        v = radio.get(key)
        if isinstance(v, list) and old in v:
            out.append(Ref(f"{env.config} [radio] {key}", f'"{old}" in {key}',
                           f"{key} = " + json.dumps([new if x == old else x for x in v])))
    if radio.get("shared_device") == old:
        out.append(Ref(f"{env.config} [radio] shared_device", f'shared_device = "{old}"', f'shared_device = "{new}"'))
    for ch in radio.get("channels", []) if isinstance(radio.get("channels"), list) else []:
        if isinstance(ch, dict) and ch.get("device") == old:
            out.append(Ref(f"{env.config} [[radio.channels]] {ch.get('label', '?')}", f'device = "{old}"',
                           f'device = "{new}"'))
    rc = (c.get("aircraft", {}) or {}).get("receiver", {}) if isinstance(c.get("aircraft"), dict) else {}
    if isinstance(rc, dict) and rc.get("sdr") == old:
        out.append(Ref(f"{env.config} [aircraft.receiver] sdr", f'sdr = "{old}"', f'sdr = "{new}"'))
    tr = _read_json(Path(env.tr_config))
    from sizeup.radio.recorder import rtl_serial
    for s in tr.get("sources", []) if isinstance(tr.get("sources"), list) else []:
        if isinstance(s, dict) and rtl_serial(s.get("device")) == old:
            out.append(Ref(f"{env.tr_config} (from the release's deploy/trunk-recorder/config.json)",
                           f'"device": "{s.get("device")}"',
                           f'"device": "{str(s.get("device")).replace("rtl=" + old, "rtl=" + new)}" in '
                           "deploy/trunk-recorder/config.json (sizeup hands trunk-recorder the dongle's index itself "
                           "when Fire/EMS 15 is a dongle's job)"))
    return out


def meant(old: str, target: tuple[int, str, str], devs: list[tuple[int, str, str]], products: dict) -> str:
    """Which physical dongle the old serial's references meant: "renamed" (the one being renamed), "other" (the
    other one with that serial), or "unknown"."""
    same = [(i, p) for i, p, s in devs if s == old]
    if len(same) <= 1:
        return "renamed"                              # its serial was its own: everything that named it meant it
    was = products.get(old)
    match = [i for i, p in same if was and p == was]
    if len(match) != 1:
        return "unknown"                              # no product on record, or the same product twice
    return "renamed" if match[0] == target[0] else "other"


def move_state(env: Env, refs: list[Ref], old: str, new: str) -> list[str]:
    """Move sizeup's own state from `old` to `new` (the job, the ADS-B choice, the product on record)."""
    done = []
    if any(r.state == "modes" for r in refs):
        p = env.modes_file()
        d = _read_json(p)
        if old in d:
            d[new] = d.pop(old)
            by = d.get("_set_by") if isinstance(d.get("_set_by"), dict) else {}
            if old in by:
                by[new] = by.pop(old)
            _write_json(p, d)
            done.append(f"{p}: the job {d[new]} moved from {old} to {new}")
    if any(r.state == "adsb" for r in refs):
        p = env.adsb_file()
        d = _read_json(p)
        if d.get("sdr") == old:
            d.update(sdr=new, at=env.clock(), by="sdr-serial")
            _write_json(p, d)
            done.append(f"{p}: the ADS-B receiver is now {new}")
    return done


# -- the steps ------------------------------------------------------------------------------------------------------


class Abort(Exception):
    pass


def _systemctl(env: Env, *args: str) -> subprocess.CompletedProcess:
    return env.run(["systemctl", *args], timeout=90)


def stop_ours(env: Env) -> list[str]:
    """Stop our services and hold the timers; what was running (to start again)."""
    if not env.systemd:
        return []
    active = [u for u in SERVICES + HOLD if _systemctl(env, "is-active", "--quiet", u).returncode == 0]
    if _systemctl(env, "is-active", "--quiet", "sizeup-update.service").returncode == 0:
        raise Abort("an automatic update is running (journalctl -u sizeup-update -f): try again when it's done")
    if active:
        env.say("Stopping " + ", ".join(active) + " (rtl_fm stops with sizeup-station)")
        r = _systemctl(env, "stop", *active)
        if r.returncode != 0:
            raise Abort(f"systemctl stop failed: {(r.stderr or r.stdout).strip()[-300:]}")
    for _ in range(10):
        r = env.run(["pgrep", "-u", "sizeup", "-x", CHILDREN], timeout=10)
        if r.returncode != 0:
            break
        env.sleep(1.0)
    else:
        raise Abort(f"{CHILDREN.replace('|', ', ')} still running as sizeup (pids {' '.join(r.stdout.split())}): "
                    "stop them, then try again")
    return active


def start_ours(env: Env, active: list[str]) -> None:
    if active:
        r = _systemctl(env, "start", *active)
        env.say("Started " + ", ".join(active) + ("" if r.returncode == 0 else
                                                   f" (FAILED: {(r.stderr or r.stdout).strip()[-200:]})"))


def wait_for_claims(env: Env, serial: str) -> None:
    """Another app's live claim on this serial (shortfinal): ask the person to switch it Off there, never kill it."""
    while True:
        held = [c for d in env.claims_dirs if (c := adsbd.holder(serial, d))]
        mine = [c for c in held if c.get("app") == "sizeup"]
        if mine:
            raise Abort(f"a sizeup process (pid {mine[0].get('pid')}) still holds {serial}: stop it, then try again")
        if not held:
            return
        c = held[0]
        env.say(f"{c.get('app', 'another app')} holds {serial} ({c.get('mode', '?')}, pid {c.get('pid')}): switch that "
                f"dongle Off in {c.get('app', 'it')} (it is never stopped from here), then press Enter; q gives up.")
        try:
            a = env.ask("> ")
        except EOFError:
            a = "q"
        if a.strip().lower() in ("q", "quit", "n", "no"):
            raise Abort(f"{c.get('app', 'another app')} still holds {serial}: nothing written")


def backup_path(env: Env, old: str) -> Path:
    day = datetime.fromtimestamp(env.clock()).strftime("%Y-%m-%d")
    p = env.backups / f"{old}-{day}.bin"
    if p.exists():
        p = env.backups / f"{old}-{day}-{datetime.fromtimestamp(env.clock()).strftime('%H%M%S')}.bin"
    return p


def backup(env: Env, idx: int, old: str) -> Path:
    env.backups.mkdir(parents=True, exist_ok=True)
    p = backup_path(env, old)
    env.say(f"Backing up #{idx}'s EEPROM: {env.rtl_eeprom} -d {idx} -r {p}")
    r = env.run([env.rtl_eeprom, "-d", str(idx), "-r", str(p)], stdin=subprocess.DEVNULL, timeout=60)
    try:
        data = p.read_bytes()
    except OSError:
        data = b""
    # (its exit code isn't trusted either way: rtl_eeprom returns what its last call returned, a byte count for a
    # dump; the file is what counts: the whole EEPROM, starting with the RTL2832's 0x28 0x32)
    if len(data) < EEPROM_MIN or data[:2] != b"\x28\x32":
        why = ("no backup file" if not data else f"the backup isn't an RTL2832 EEPROM ({len(data)} bytes)")
        raise Abort(f"EEPROM backup failed ({why}; rtl_eeprom exit {r.returncode}: "
                    f"{(r.stderr or r.stdout or '').strip()[-300:]}): nothing written")
    who = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if all(who):
        try:
            os.chown(env.backups, int(who[0]), int(who[1]))
            os.chown(p, int(who[0]), int(who[1]))
        except (OSError, ValueError):
            pass
    env.say(f"  backup: {p} ({len(data)} bytes). To undo: rtl_eeprom -d <index> -w {p}")
    return p


def write(env: Env, idx: int, new: str) -> None:
    env.say(f"Writing the serial: {env.rtl_eeprom} -d {idx} -s {new}")
    r = env.run([env.rtl_eeprom, "-d", str(idx), "-s", new], input="y\n", timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    if "successfully written" not in out.lower():
        raise Abort(f"rtl_eeprom didn't write it (exit {r.returncode}): {out.strip()[-400:]}")


def wait_replug(env: Env, target: tuple[int, str, str], new: str, old_count: int, timeout: float) -> list:
    idx, product, old = target
    env.say(f"Now unplug dongle #{idx} ({product}, the one just written) and plug it back in"
            + (f"; leave the other {old} plugged in" if old_count > 1 else "")
            + ". (WSL: sizeup-usb attaches it again within ~10 s.) Waiting for it...")
    until, said = env.clock() + timeout, env.clock()
    while True:
        devs = env.devices()
        n_new = sum(1 for _i, _p, s in devs if s == new)
        if n_new == 1 and sum(1 for _i, _p, s in devs if s == old) == old_count - 1:
            return devs
        if n_new > 1:
            raise Abort(f"{new} shows up {n_new} times: {listing(devs)}")
        if env.clock() >= until:
            raise Abort(f"{new} hasn't shown up in {timeout:.0f} s (now plugged in:\n{listing(devs)}\n): replug it, then "
                        "check with sizeup-station status. The EEPROM is written; the backup is kept")
        if env.clock() - said >= 30:
            env.say("  still waiting for it (unplug it, wait a second, plug it back in)...")
            said = env.clock()
        env.sleep(POLL_S)


def jobs_now(env: Env, devs: list[tuple[int, str, str]], wait_s: float = 60.0) -> list[str]:
    """Which job runs on which dongle now, from the station page's API (else the saved jobs)."""
    until = env.clock() + wait_s
    st = None
    while env.systemd and env.clock() < until:
        st = env.api("/api/state")
        rows = ((st or {}).get("sdr") or {}).get("sdrs")
        if rows and not (st.get("sdr") or {}).get("switching"):
            break
        env.sleep(POLL_S)
    sd = (st or {}).get("sdr") or {}
    rows = {r.get("serial"): r for r in sd.get("sdrs") or [] if isinstance(r, dict)}
    modes = _read_json(env.modes_file())
    adsb = (sd.get("adsb") or {}).get("serial") or _read_json(env.adsb_file()).get("sdr") or \
        ((env.conf().get("aircraft") or {}).get("receiver") or {}).get("sdr")
    out = []
    for i, p, s in devs:
        r = rows.get(s) or {}
        job = "ADS-B (readsb)" if s and s == adsb else r.get("mode") or modes.get(s) or "Off"
        if job == "Fire/EMS 15":
            job += " (trunk-recorder)"
        name = r.get("name") if r.get("name") and r.get("name") != s else ""
        how = " · auto" if r.get("set_by") == "auto" else ""
        out.append(f"  #{i}  {s or '(no serial)'}{f' ({name})' if name else ''}  {p}: {job}{how}"
                   + (f"  [{r['ambiguous']}]" if r.get("ambiguous") else ""))
    if not rows:
        out.append("  (the station page didn't answer yet: these are the saved jobs; sizeup-station status shows them live)")
    return out


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sizeup-station sdr-serial",
                                 description="Give one RTL-SDR a new, unique USB serial (rtl_eeprom), safely.")
    ap.add_argument("dongle", help="its index (sizeup-station status, 'dongles') or its current serial")
    ap.add_argument("new", help="the new serial: 3-16 of A-Z a-z 0-9 -, with a letter (e.g. hpn-twr, adsb1090)")
    ap.add_argument("--dry-run", action="store_true", help="show the plan; nothing stopped or written")
    ap.add_argument("--yes", action="store_true", help="don't ask before writing")
    ap.add_argument("--wait", type=float, default=REPLUG_S, help="seconds to wait for the replug (default 300)")
    a = ap.parse_args(argv)
    env = env or Env()
    say = env.say
    if not a.dry_run and not env.is_root():
        say("run it as root: sudo sizeup-station sdr-serial ...")
        return 2
    devs = env.devices()
    if not devs:
        say("librtlsdr lists no dongle: plugged in? (WSL: usbipd list says Attached?)")
        return 1
    products = _read_json(env.products_file())
    target, why = pick(a.dongle, devs, products)
    if target is None:
        say(f"{why}. Plugged in now:\n{listing(devs)}")
        return 2
    idx, product, old = target
    if why := serial_problem(a.new, devs, old):
        say(f"{a.new!r}: {why}")
        return 2
    if not a.dry_run and not os.access(env.rtl_eeprom, os.X_OK):
        say(f"{env.rtl_eeprom} isn't installed (sudo sizeup-station update installs it)")
        return 1
    old_count = sum(1 for _i, _p, s in devs if s == old)
    refs = references(env, old, a.new)
    which = meant(old, target, devs, products)
    others = [(i, p) for i, p, s in devs if s == old and i != idx]
    say(f"Dongle #{idx}: {product}, serial {old}" + (f" (shared with #{', #'.join(str(i) for i, _p in others)})"
                                                     if others else "") + f"  ->  {a.new}")
    say("Plan:\n"
        f"  1. stop {', '.join(SERVICES)} (rtl_fm with sizeup-station); hold {', '.join(HOLD)}\n"
        f"  2. wait for any other app holding {old} (shortfinal) to let go: never killed\n"
        f"  3. back up: {env.rtl_eeprom} -d {idx} -r {backup_path(env, old)}   (no backup, no write)\n"
        f"  4. write:   {env.rtl_eeprom} -d {idx} -s {a.new}   (answering its y/n)\n"
        f"  5. you unplug #{idx} and plug it back in; then {a.new} must show up once\n"
        "  6. references (below), then start what was stopped and list the jobs")
    say(plan_refs(refs, which, old, a.new, target, others))
    if a.dry_run:
        say("Dry run: nothing stopped or written.")
        return 0
    if not a.yes:
        try:
            ok = env.ask(f"Write serial {a.new} to #{idx} ({product})? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            ok = False
        if not ok:
            say("Nothing done.")
            return 1
    active: list[str] = []
    rc = 0
    try:
        active = stop_ours(env)
        wait_for_claims(env, old)
        now = env.devices()                          # the same dongle at the same index, still?
        if next((d for d in now if d[0] == idx), None) != target:
            raise Abort(f"the dongles changed while stopping (now:\n{listing(now)}\n): run it again")
        backup(env, idx, old)
        write(env, idx, a.new)
        devs2 = wait_replug(env, target, a.new, old_count, a.wait)
        say(f"OK: {a.new} is on one dongle" + (f"; {old} is on #{', #'.join(str(i) for i, _p, s in devs2 if s == old)}"
                                                if old_count > 1 else ""))
        left = sum(1 for _i, _p, s in devs2 if s == old)
        if left > 1:
            say(f"  {old} is still on {left} dongles: run this again for one of them")
        if which == "renamed":
            for line in move_state(env, refs, old, a.new):
                say("  " + line)
    except Abort as e:
        say(f"Stopped: {e}")
        rc = 1
        devs2 = env.devices()
    finally:
        start_ours(env, active)
    if rc == 0:
        say("Jobs now:")
        for line in jobs_now(env, devs2):
            say(line)
    return rc


def plan_refs(refs: list[Ref], which: str, old: str, new: str, target: tuple, others: list) -> str:
    """What happens to what names the old serial, and what to set by hand."""
    idx, product, _ = target
    if not refs:
        return f"Nothing in sizeup's config or state names {old}."
    own = [r for r in refs if r.state]
    conf = [r for r in refs if not r.state]
    lines = [f"What names {old} now:"] + [f"  {r.where}: {r.what}" for r in refs]
    if which == "renamed":
        why = ("its serial is its own" if not others else
               f"it has the product {old} had before the other one turned up ({product})")
        lines.append(f"Those meant this dongle (#{idx}: {why}), so they go with it to {new}:")
        lines += [f"  {r.where}: moved to {new}" for r in own]
        if conf:
            lines.append("  Set by hand (sudo nano /etc/sizeup/config.toml, then sudo sizeup-station restart):")
            lines += [f"    {r.where}: {r.fix}" for r in conf]
    elif which == "other":
        j = ", ".join(f"#{i} ({p})" for i, p in others)
        lines.append(f"Those meant the other dongle with {old}, {j} (the product on record for {old}): left as they "
                     f"are; {old} is that dongle's alone after this. Nothing to set.")
    else:
        j = ", ".join(f"#{i} ({p})" for i, p in others)
        lines.append(f"Which dongle those meant can't be told ({old} had no product on record, or both are the same "
                     f"model): nothing is changed. If they meant {j}, nothing to do. If they meant this one (#{idx}, "
                     f"{product}), set after the rename:")
        lines += [f"    {r.where}: {r.fix}" for r in conf]
        lines += [f"    {r.where}: pick {r.fix} on the station page (Radios) or the hub's Settings > Radios"
                  for r in own]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
