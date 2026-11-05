"""The station's ADS-B decoder (GitHub #235): readsb on the ONE dongle named for ADS-B, and nothing else.

The ADS-B dongle has a 1090 MHz filter and an LNA in front of it (user, #235: "this one we will have to be specific
about and cant auto assign because itll have a filter LNA in line"). So it is chosen by its exact USB serial only:
[aircraft.receiver] sdr in /etc/sizeup/config.toml, or Settings > Radios (an admin, with a confirm), which sizeup-station
keeps in OVERRIDE. sizeup's own radios and auto-assign never take it (sizeup/app.py: it is ignored there), and this
never opens any other dongle. With no dongle named, nothing runs and no dongle is touched.

    sizeup-adsb.service (deploy/station/systemd): the sizeup user runs, with the system python3 (stdlib only, like
    sizeup/cloud/autoupdate.py; installed as /usr/local/lib/sizeup/adsbd.py by install-linux.sh):
        python3 -I adsbd.py run        supervise readsb (the service)
        python3 -I adsbd.py print      the readsb command line it would run now, or why none
        python3 -I adsbd.py status     the last status it wrote

What runs: readsb (wiedehopf, pinned and built by install-linux.sh, /opt/sizeup/readsb/bin/readsb) with
    --device-type rtlsdr --device <index>   the dongle's CURRENT index, looked up by its exact serial right before each
                                            start (sizeup/radio/sdr.py's rule: readsb also matches a serial by prefix
                                            or suffix, so a serial is never passed)
    --gain auto | <dB>                      [aircraft.receiver] gain; auto by default (shortfinal, 2026-10-02: an LNA
                                            in front makes a fixed high gain overload the dongle)
    --enable-biastee                        only with [aircraft.receiver] bias_tee = true: 4.5 V up the coax for an LNA
                                            that is powered that way. Off by default: an LNA with its own supply, or a
                                            plain filter, must not get it
    --lat/--lon HPN's reference point       for decoding surface positions (not the station's own location)
    --write-json JSON_DIR --write-json-every 1   aircraft.json every second, read by the station's forwarder
                                            (sizeup/cloud/aircraftlink.py) and sent on to the hub
No network ports: readsb's networking stays off.

The shared dongle claims (~/.local/state/rtl-claims, sizeup/radio/claims.py, agreed with shortfinal): while readsb
runs, this process holds the dongle's claim (app "sizeup", mode "adsb"). If another app (shortfinal) holds it, nothing
starts: the status says who has it, and it is retried every few seconds until they let it go. It never fights for it.

Supervision (shortfinal's core/supervisor.py, simplified): a readsb that exits is restarted with backoff (2 s doubling
to 30 s); one whose aircraft.json stops updating, or whose dongle stops delivering samples, is restarted at once; one
that decodes nothing for NO_SIGNAL_S says "no signal" (an antenna or LNA fault, or a quiet sky) and is restarted on a
slow schedule. A change of the dongle, gain or bias-tee (the config, or Settings) restarts it with the new settings.

Two dongles with the dongle's serial (#242): nothing starts (state "ambiguous", the fix in its detail) until one is
renamed: readsb never guesses between them.

The status (STATUS, every STEP_S, atomically): {at, state: off | refused | missing | held | unplugged | ambiguous |
starting | ok | no_signal | down, detail, sdr, source: config | settings, gain, bias_tee, pid, index, since, restarts, held_by,
messages_per_s, aircraft, with_pos, last_message_at, json, argv}. sizeup-station reads it for Settings > Radios and the
station health it sends the hub (sizeup/cloud/health.py)."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

CONFIG = "/etc/sizeup/config.toml"
OVERRIDE = "/var/lib/sizeup/state/adsb_receiver.json"      # Settings > Radios (sizeup/app.py set_adsb_receiver)
RUN_DIR = "/run/sizeup-adsb"                               # the service's RuntimeDirectory (tmpfs)
JSON_DIR = RUN_DIR + "/json"                               # readsb's --write-json
AIRCRAFT_JSON = JSON_DIR + "/aircraft.json"
STATUS = RUN_DIR + "/decoder.json"
READSB = "/opt/sizeup/readsb/bin/readsb"
LIBRTLSDR = ("/opt/sizeup/rtl-sdr/lib/librtlsdr.so.0", "librtlsdr.so.0", "/opt/homebrew/lib/librtlsdr.dylib")
REF = (41.06695, -73.70757)       # HPN's airport reference point (sizeup/aircraft.py ARP), for surface positions
APP, MODE = "sizeup", "adsb"
SERIAL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
GAINS = (0.0, 60.0)               # an R820T's range in dB (readsb rounds to the nearest step)

STEP_S = 2.0
GRACE_S = 20.0                    # don't judge a fresh start
STALE_S = 10.0                    # aircraft.json not rewritten this long: readsb is stuck, restart
STALL_S = 60.0                    # the dongle's sample count not moving this long: deaf, restart
NO_SIGNAL_S = 120.0               # nothing decoded this long: "no signal"
NO_SIGNAL_RESTART_S = (300.0, 1800.0)
BACKOFF_S = (2.0, 30.0)
RATE_WINDOW_S = 60.0              # messages / s over the last minute


def gain_problem(gain) -> str:
    """"" for a gain readsb takes ("auto", or dB in GAINS), else what is wrong."""
    if isinstance(gain, str) and gain.strip().lower() == "auto":
        return ""
    try:
        g = float(gain)
    except (TypeError, ValueError):
        return 'auto (the default) or a number of dB, e.g. 40'
    return "" if GAINS[0] <= g <= GAINS[1] else f"{GAINS[0]:g} to {GAINS[1]:g} dB, or auto"


def serial_problem(serial: str) -> str:
    """"" for a serial the decoder may use, else why not (sizeup/config.py says the same)."""
    if not serial:
        return ""
    if len(serial) > 32 or not set(serial) <= SERIAL_CHARS:
        return "the dongle's exact USB serial: letters, digits, . _ - (at most 32)"
    if serial.isdigit():
        return ("an all-digit serial is read as a device index by readsb and the rtl tools: give the dongle a name "
                "first (rtl_eeprom -d <index> -s adsb1090)")
    return ""


# -- what to run -----------------------------------------------------------------------------------------------------


def settings(config: str = CONFIG, override: str = OVERRIDE) -> dict:
    """{sdr, source, gain, bias_tee, problem}: the config's [aircraft.receiver], the dongle as Settings named it
    (OVERRIDE {"sdr": ...}, "" = none) over the config's."""
    try:
        with open(config, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        data, err = {}, f"{config}: {type(e).__name__}"
    else:
        err = ""
    ac = data.get("aircraft") if isinstance(data.get("aircraft"), dict) else {}
    rc = ac.get("receiver") if isinstance(ac.get("receiver"), dict) else {}
    sdr, source = str(rc.get("sdr") or "").strip(), "config" if rc.get("sdr") else ""
    try:
        ov = json.loads(Path(override).read_text())
    except (OSError, ValueError):
        ov = None
    if isinstance(ov, dict) and isinstance(ov.get("sdr"), str):
        sdr, source = ov["sdr"].strip(), "settings"
    gain = rc.get("gain", "auto")
    gain = "auto" if isinstance(gain, bool) or gain_problem(gain) or str(gain).strip().lower() == "auto" else f"{float(gain):g}"
    bias = rc.get("bias_tee", False) is True
    problem = serial_problem(sdr) or (f"the config can't be read ({err})" if err and not sdr else "")
    return {"sdr": sdr, "source": source, "gain": gain, "bias_tee": bias, "problem": problem}


def readsb_argv(binary: str, index: int, gain: str = "auto", bias_tee: bool = False, json_dir: str = JSON_DIR,
                lat: float = REF[0], lon: float = REF[1]) -> list[str]:
    """readsb on device `index` (resolved from the exact serial just before), aircraft.json every second."""
    argv = [binary, "--device-type", "rtlsdr", "--device", str(int(index)), "--gain", str(gain),
            "--lat", f"{lat:.5f}", "--lon", f"{lon:.5f}", "--write-json", json_dir, "--write-json-every", "1"]
    if bias_tee:
        argv.append("--enable-biastee")
    return argv


# -- the dongles (librtlsdr's USB strings: nothing is opened) -----------------------------------------------------------

_lib = None


def _load():
    global _lib
    if _lib is None:
        for cand in LIBRTLSDR:
            try:
                _lib = ctypes.CDLL(cand)
                break
            except OSError:
                continue
        else:
            _lib = False
    return _lib or None


def devices() -> list[tuple[int, str]] | None:
    """[(index, serial)] plugged in now; None when librtlsdr can't be loaded."""
    lib = _load()
    if lib is None:
        return None
    out = []
    for i in range(int(lib.rtlsdr_get_device_count())):
        m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
        if lib.rtlsdr_get_device_usb_strings(i, m, p, s) == 0:
            out.append((i, s.value.decode(errors="replace")))
    return out


def duplicate_text(serial: str, indexes: list[int]) -> str:
    """sizeup/radio/sdr.py duplicate_text()'s words (this file is stdlib only, installed on its own)."""
    words = {2: "Two", 3: "Three", 4: "Four"}
    ix = [str(i) for i in indexes]
    listed = ix[0] if len(ix) == 1 else ", ".join(ix[:-1]) + " and " + ix[-1]
    return (f"{words.get(len(ix), str(len(ix)))} dongles share serial {serial} (indexes {listed}): sizeup can't tell "
            f"them apart. Fix: sizeup-station sdr-serial {ix[-1]} <new-serial>")


def resolve(serial: str, found: list[tuple[int, str]] | None) -> int | None:
    """The index of the one dongle whose serial is exactly `serial`; None if it isn't there (or twice)."""
    hits = [i for i, s in (found or []) if s == serial]
    return hits[0] if len(hits) == 1 else None


# -- the shared claims (sizeup/radio/claims.py's format; stdlib only here) ------------------------------------------------


def claims_dir() -> Path:
    return Path.home() / ".local" / "state" / "rtl-claims"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, OverflowError):
        return False


def holder(serial: str, d: Path | None = None) -> dict | None:
    """The live claim on this dongle (any app), or None. A stale one (its pid gone) is removed."""
    p = (d or claims_dir()) / f"{serial}.json"
    try:
        c = json.loads(p.read_text())
        pid = int(c["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if _alive(pid):
        return c
    try:
        p.unlink()
    except OSError:
        pass
    return None


def other(serial: str, d: Path | None = None) -> dict | None:
    """A live claim by anyone but this process."""
    c = holder(serial, d)
    return c if c and (c.get("app") != APP or int(c.get("pid", -1)) != os.getpid()) else None


def claim(serial: str, d: Path | None = None) -> bool:
    if other(serial, d):
        return False
    d = d or claims_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{serial}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"app": APP, "pid": os.getpid(), "mode": MODE, "since": time.time()}))
        os.replace(tmp, d / f"{serial}.json")
    except OSError:
        pass                          # can't record it: still run (the dongle itself is the real lock)
    return True


def release(serial: str, d: Path | None = None) -> None:
    c = holder(serial, d)
    if c and c.get("app") == APP and int(c.get("pid", -1)) == os.getpid():
        try:
            ((d or claims_dir()) / f"{serial}.json").unlink()
        except OSError:
            pass


# -- health from readsb's own output ---------------------------------------------------------------------------------------


class Output:
    """readsb's aircraft.json (rewritten every second: "now", "messages" = the total decoded) and stats.json (the
    dongle's samples): messages / s over the last minute, aircraft (with a position), the last message's time."""

    def __init__(self, json_dir: str = JSON_DIR):
        self.dir = Path(json_dir)
        self.reset()

    def reset(self) -> None:
        self.counts: list[tuple[float, int]] = []          # (time, messages total)
        self.last_message_at: float | None = None
        self.samples: tuple[float, float] | None = None    # (value, when it last moved)

    def read(self, now: float) -> dict:
        try:
            ac = json.loads((self.dir / "aircraft.json").read_text())
        except (OSError, ValueError) as e:
            return {"ok": False, "why": f"no aircraft.json yet ({type(e).__name__})"}
        at = float(ac.get("now") or 0)
        msgs = int(ac.get("messages") or 0)
        planes = [a for a in ac.get("aircraft") or [] if isinstance(a, dict)]
        if not self.counts or msgs != self.counts[-1][1]:
            if self.counts and msgs > self.counts[-1][1]:
                self.last_message_at = at
            self.counts.append((at, msgs))
        self.counts = [c for c in self.counts if at - c[0] <= RATE_WINDOW_S] or self.counts[-1:]
        rate = None
        if len(self.counts) >= 2 and self.counts[-1][0] > self.counts[0][0]:
            rate = max(0.0, (self.counts[-1][1] - self.counts[0][1]) / (self.counts[-1][0] - self.counts[0][0]))
        try:
            st = json.loads((self.dir / "stats.json").read_text())
            samples = float(((st.get("total") or {}).get("local") or {}).get("samples_processed") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            samples = None
        if samples is not None and (self.samples is None or samples != self.samples[0]):
            self.samples = (samples, now)
        return {"ok": True, "age": now - at, "messages_per_s": None if rate is None else round(rate, 1),
                "aircraft": len(planes), "with_pos": sum(1 for a in planes if "lat" in a and "lon" in a),
                "last_message_at": self.last_message_at,
                "samples_still_s": None if self.samples is None else now - self.samples[1]}


# -- the supervisor --------------------------------------------------------------------------------------------------------


class Decoder:
    """One step every STEP_S (step(now)); all state in self.status. popen / found / clock are for the tests."""

    def __init__(self, config: str = CONFIG, override: str = OVERRIDE, binary: str = READSB, json_dir: str = JSON_DIR,
                 status_path: str = STATUS, claims: Path | None = None, popen=subprocess.Popen, found=devices,
                 log=lambda m: print(m, flush=True)):
        self.config, self.override, self.binary, self.json_dir = config, override, binary, json_dir
        self.status_path, self.claims, self.popen, self.found, self.log = status_path, claims, popen, found, log
        self.proc: subprocess.Popen | None = None
        self.running: tuple | None = None          # (sdr, gain, bias_tee) of the readsb that runs
        self.claimed = ""
        self.started_at: float | None = None
        self.next_start = 0.0
        self.fails = 0
        self.restarts = 0
        self.no_signal_since: float | None = None
        self.no_signal_wait = NO_SIGNAL_RESTART_S[0]
        self.out = Output(json_dir)
        self.status: dict = {"state": "starting", "detail": "", "since": None}

    def _set(self, now: float, state: str, detail: str, **kw) -> None:
        """The status now; `since` is when it last changed state (logged then)."""
        changed = state != self.status.get("state")
        if changed:
            self.log(f"adsb: {state}" + (f": {detail}" if detail else ""))
        self.status = {"state": state, "detail": detail, "since": now if changed else self.status.get("since"), **kw}

    def stop(self, why: str = "") -> None:
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
            except OSError:
                pass
            self.log("adsb: readsb stopped" + (f" ({why})" if why else ""))
        if self.claimed:
            release(self.claimed, self.claims)
            self.claimed = ""
        self.running, self.started_at = None, None
        self.out.reset()

    def _fail(self, now: float, why: str) -> None:
        self.fails += 1
        wait = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** (self.fails - 1))
        self.next_start = now + wait
        self.restarts += 1
        self.stop(why)
        self.log(f"adsb: {why}; trying again in {wait:.0f} s")

    def step(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        s = settings(self.config, self.override)
        want = (s["sdr"], s["gain"], s["bias_tee"])
        base = {"sdr": s["sdr"], "source": s["source"], "gain": s["gain"], "bias_tee": s["bias_tee"],
                "restarts": self.restarts, "json": str(Path(self.json_dir) / "aircraft.json")}
        if self.proc is not None and self.running != want:
            self.stop("its dongle, gain or bias-tee changed")
            self.fails, self.next_start = 0, 0.0
        if not s["sdr"]:
            self.stop("no dongle named for ADS-B")
            self._set(now, "off", "no dongle named for ADS-B ([aircraft.receiver] sdr, or Settings > Radios)", **base)
        elif s["problem"]:
            self.stop(s["problem"])
            self._set(now, "refused", s["problem"], **base)
        elif not os.access(self.binary, os.X_OK):
            self._set(now, "missing", f"{self.binary} isn't installed (sudo sizeup-station update builds it)", **base)
        elif self.proc is None:
            self._start(now, s, want, base)
        else:
            self._watch(now, s, base)
        self.status["at"] = now
        self._write()
        return self.status

    def _start(self, now: float, s: dict, want: tuple, base: dict) -> None:
        c = other(s["sdr"], self.claims)
        if c:
            held = {"app": c.get("app"), "mode": c.get("mode")}
            self._set(now, "held", f"{c.get('app', 'another app')} holds {s['sdr']}"
                                   + (f" ({c.get('mode')})" if c.get("mode") else "")
                                   + ": not started (switch it Off there to hand it over)", held_by=held, **base)
            return
        if now < self.next_start:
            self.status.update(base, detail=f"{self.status.get('detail', '').split(';')[0]}; retry in "
                                            f"{self.next_start - now:.0f} s")
            return
        try:
            found = self.found()
        except Exception as e:  # noqa: BLE001 - librtlsdr in a bad way: like not plugged in
            found = None
            self.log(f"adsb: dongle lookup failed: {type(e).__name__}: {e}")
        idx = resolve(s["sdr"], found)
        if idx is None:
            same = [i for i, x in (found or []) if x == s["sdr"]]
            self.next_start = now + 5.0
            if len(same) > 1:              # #242: never a guess between them (one may be a radio, not behind the filter)
                self._set(now, "ambiguous", duplicate_text(s["sdr"], same) + ": readsb not started", present=True, **base)
                return
            why = "librtlsdr not found" if found is None else f"{s['sdr']} isn't plugged in"
            self._set(now, "unplugged", why, present=False, **base)
            return
        if not claim(s["sdr"], self.claims):
            return                                     # taken between the look and now: held, next step says by whom
        self.claimed = s["sdr"]
        argv = readsb_argv(self.binary, idx, s["gain"], s["bias_tee"], self.json_dir)
        try:
            Path(self.json_dir).mkdir(parents=True, exist_ok=True)
            for stale in ("aircraft.json", "stats.json"):
                (Path(self.json_dir) / stale).unlink(missing_ok=True)
            self.proc = self.popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self._fail(now, f"readsb can't start: {e}")
            self._set(now, "down", f"readsb can't start: {e}", **base)
            return
        self.running, self.started_at = want, now
        self.no_signal_since = None
        self.log(f"adsb: readsb on {s['sdr']} (device #{idx}), gain {s['gain']}, bias-tee "
                 f"{'ON' if s['bias_tee'] else 'off'}, pid {self.proc.pid}")
        self._set(now, "starting", f"readsb on {s['sdr']} (device #{idx})", pid=self.proc.pid, index=idx,
                  present=True, argv=argv, **base)

    def _watch(self, now: float, s: dict, base: dict) -> None:
        keep = {k: self.status.get(k) for k in ("pid", "index", "argv")}
        code = self.proc.poll()
        if code is not None:
            self._fail(now, f"readsb exited with code {code}")
            self._set(now, "down", f"readsb exited with code {code} (unplugged? see journalctl -u sizeup-adsb)",
                      **base)
            return
        o = self.out.read(now)
        nums = {k: o.get(k) for k in ("messages_per_s", "aircraft", "with_pos", "last_message_at")}
        if now - (self.started_at or now) < GRACE_S:
            self._set(now, "starting", f"readsb on {s['sdr']}, starting", present=True, **keep, **nums, **base)
            return
        if not o["ok"] or o["age"] > STALE_S:
            why = o.get("why") or f"aircraft.json not updated for {o['age']:.0f} s"
            self._fail(now, f"readsb stuck: {why}")
            self._set(now, "down", f"readsb stuck ({why}): restarting", **base)
            return
        if o["samples_still_s"] is not None and o["samples_still_s"] > STALL_S:
            self._fail(now, f"no samples from the dongle for {o['samples_still_s']:.0f} s")
            self._set(now, "down", "the dongle stopped delivering samples: restarting", **base)
            return
        quiet = now - (o["last_message_at"] or self.started_at or now)
        if quiet > NO_SIGNAL_S:
            if self.no_signal_since is None:
                self.no_signal_since = now
            elif now - self.no_signal_since >= self.no_signal_wait:
                self.no_signal_wait = min(self.no_signal_wait * 2, NO_SIGNAL_RESTART_S[1])
                self.no_signal_since = None
                self.restarts += 1
                self.stop("no signal")
                self._set(now, "down", f"nothing decoded for {quiet:.0f} s: restarting readsb", **base)
                return
            self._set(now, "no_signal", f"nothing decoded for {quiet:.0f} s: check the antenna, the filter and the LNA"
                                        + (" (bias-tee is on)" if s["bias_tee"] else " (bias-tee is off: does the LNA "
                                                                                     "need power from it?)"),
                      present=True, **keep, **nums, **base)
            return
        self.no_signal_since, self.no_signal_wait = None, NO_SIGNAL_RESTART_S[0]
        if self.fails and now - (self.started_at or now) > 120:
            self.fails = 0
        rate = o["messages_per_s"]
        self._set(now, "ok", f"{o['aircraft']} aircraft ({o['with_pos']} with a position)"
                             + (f", {rate:g} messages/s" if rate is not None else ""),
                  present=True, **keep, **nums, **base)

    def _write(self) -> None:
        try:
            p = Path(self.status_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.{os.getpid()}")
            tmp.write_text(json.dumps(self.status, separators=(",", ":")))
            os.chmod(tmp, 0o644)
            os.replace(tmp, p)
        except OSError:
            pass


def read_status(path: str | None = None, now: float | None = None) -> dict | None:
    """The decoder's last status (sizeup-station: Settings > Radios, station health); None when there is none. One older
    than 30 s says so: the service isn't running."""
    try:
        st = json.loads(Path(path or STATUS).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None
    now = time.time() if now is None else now
    if isinstance(st.get("at"), (int, float)) and now - st["at"] > 30:
        st = dict(st, state="down", detail=f"the decoder service hasn't reported for {now - st['at']:.0f} s "
                                           "(systemctl status sizeup-adsb)")
    return st


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "run"
    if cmd == "print":
        s = settings()
        if not s["sdr"] or s["problem"]:
            print(s["problem"] or "no dongle named for ADS-B: nothing runs")
            return 0
        idx = resolve(s["sdr"], devices())
        print(" ".join(readsb_argv(READSB, idx if idx is not None else -1, s["gain"], s["bias_tee"]))
              + ("" if idx is not None else f"   ({s['sdr']} isn't plugged in now)"))
        return 0
    if cmd == "status":
        st = read_status()
        print(json.dumps(st, indent=2) if st else "no status yet (is sizeup-adsb running?)")
        return 0
    if cmd != "run":
        print("usage: adsbd.py run | print | status", file=sys.stderr)
        return 2
    d = Decoder()
    stopping = []

    def bye(*_a):
        stopping.append(1)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    d.log(f"adsb: decoder supervisor up ({READSB}); the dongle named in {CONFIG} [aircraft.receiver] sdr or Settings")
    try:
        while not stopping:
            try:
                d.step()
            except Exception as e:  # noqa: BLE001 - supervision must never die
                d.log(f"adsb: {type(e).__name__}: {e}")
            for _ in range(int(STEP_S * 10)):
                if stopping:
                    break
                time.sleep(0.1)
    finally:
        d.stop("the service is stopping")
        d.status.update(state="off", detail="the decoder service stopped", at=time.time())
        d._write()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
