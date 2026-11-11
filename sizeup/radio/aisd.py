"""The station's AIS decoder (GitHub #402): AIS-catcher on the ONE dongle named for AIS, and nothing else.

Off until a dongle and a marine VHF antenna are installed (deploy/station/README.md "AIS receiver"): [vessels.receiver]
enabled = true and sdr = "<exact USB serial>" in /etc/sizeup/config.toml. Off, or with no dongle named, nothing runs
and no dongle is touched. sizeup's own radios never take that dongle (sizeup/app.py).

    sizeup-ais.service (deploy/station/systemd): the sizeup user runs, with the system python3 (stdlib only, like
    adsbd.py; installed as /usr/local/lib/sizeup/aisd.py by install-linux.sh):
        python3 -I aisd.py run        supervise AIS-catcher (the service)
        python3 -I aisd.py print      the AIS-catcher command line it would run now, or why none
        python3 -I aisd.py status     the last status it wrote

What runs: AIS-catcher (jvde-github, GPLv3; installed by hand: deploy/station/README.md) with
    -d:<index>                      the dongle's CURRENT index, looked up by its exact serial right before each start
                                    (adsbd.py's rule: a serial is never passed, and two dongles with it start nothing)
    -gr rtlagc on tuner <auto|dB>   [vessels.receiver] gain (AIS-catcher's suggested start: rtlagc on, tuner auto)
    -p <ppm>                        [vessels.receiver] ppm (0 for a TCXO dongle)
    -o 5                            every decoded message as one JSON line on stdout (JSON_FULL, gpsd-style names)
No network ports: AIS-catcher's web viewer, UDP and TCP outputs stay off.

Its stdout is read here. For each MMSI the latest message of each kind is kept ("pos": types 1, 2, 3, 18, 19, 27;
"s5": type 5; "s24a" / "s24b": type 24 parts A and B; type 19 carries static data too), stamped with "t" (when it was
heard) and cut to the fields sizeup/vessels.py from_aiscatcher() reads. Positions go after POS_KEEP_S, static data
after STATIC_KEEP_S, at most MAX_MSGS. Every WRITE_S MESSAGES is written atomically: {"at", "msgs": [...]}, read by the
station's forwarder (sizeup/cloud/vessellink.py), which posts the vessels to the hub.

The shared dongle claims (~/.local/state/rtl-claims, sizeup/radio/claims.py's format, agreed with shortfinal): while
AIS-catcher runs this process holds the claim (app "sizeup", mode "ais"). Another app holding it: nothing starts, the
status says who, and it is tried again every few seconds. It never fights for it.

A decoder that exits is restarted with backoff (2 s doubling to 60 s). Nothing heard for NO_SIGNAL_S says "no signal"
(an antenna fault, or a quiet harbour at night): it keeps running.

The status (STATUS, every STEP_S, atomically): {at, state: off | missing | held | ambiguous | starting | ok |
no_signal | down, detail, sdr, pid, since, restarts, held_by, messages_per_s, vessels, last_message_at, argv}."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections import deque
from pathlib import Path

CONFIG = "/etc/sizeup/config.toml"
RUN_DIR = "/run/sizeup-ais"                                # the service's RuntimeDirectory (tmpfs)
MESSAGES = RUN_DIR + "/messages.json"
STATUS = RUN_DIR + "/decoder.json"
AIS_CATCHER = "/opt/sizeup/ais-catcher/bin/AIS-catcher"   # else AIS-catcher on PATH
LIBRTLSDR = ("/opt/sizeup/rtl-sdr/lib/librtlsdr.so.0", "librtlsdr.so.0", "/opt/homebrew/lib/librtlsdr.dylib")
APP, MODE = "sizeup", "ais"
SERIAL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")

STEP_S = 2.0
WRITE_S = 2.0
BACKOFF_S = (2.0, 60.0)
NO_SIGNAL_S = 600.0
POS_KEEP_S = 600.0
STATIC_KEEP_S = 6 * 3600.0
MAX_MSGS = 2000
RATE_WINDOW_S = 60.0
POS_TYPES = (1, 2, 3, 18, 19, 27)
FIELDS = ("type", "mmsi", "lat", "lon", "speed", "course", "heading", "status", "shipname", "callsign", "shiptype",
          "to_bow", "to_stern", "to_port", "to_starboard", "destination", "imo", "partno")


def gain_problem(gain) -> str:
    if isinstance(gain, str) and gain.strip().lower() == "auto":
        return ""
    try:
        g = float(gain)
    except (TypeError, ValueError):
        return "auto or a number of dB"
    return "" if 0.0 <= g <= 50.0 else "0 to 50 dB"


def serial_problem(serial: str) -> str:
    if not serial:
        return ""
    if len(serial) > 32 or not set(serial) <= SERIAL_CHARS:
        return f"{serial!r} is not a dongle serial (letters, digits, . _ -)"
    if serial.isdigit():
        return f"{serial}: an all-digit serial reads as a device index; name it first (rtl_eeprom -s ais162)"
    return ""


def settings(config: str = CONFIG) -> dict:
    """{enabled, sdr, gain, ppm, problem}: the config's [vessels.receiver]."""
    try:
        with open(config, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return {"enabled": False, "sdr": "", "gain": "auto", "ppm": 0, "problem": f"{config}: {type(e).__name__}"}
    vs = data.get("vessels") if isinstance(data.get("vessels"), dict) else {}
    rc = vs.get("receiver") if isinstance(vs.get("receiver"), dict) else {}
    sdr = str(rc.get("sdr") or "").strip()
    gain = rc.get("gain", "auto")
    gain = "auto" if isinstance(gain, bool) or gain_problem(gain) or str(gain).strip().lower() == "auto" else f"{float(gain):g}"
    ppm = rc.get("ppm", 0)
    ppm = ppm if isinstance(ppm, int) and not isinstance(ppm, bool) and -150 <= ppm <= 150 else 0
    return {"enabled": rc.get("enabled", False) is True, "sdr": sdr, "gain": gain, "ppm": ppm,
            "problem": serial_problem(sdr)}


def binary() -> str:
    return AIS_CATCHER if os.access(AIS_CATCHER, os.X_OK) else (shutil.which("AIS-catcher") or "")


def ais_argv(binary_: str, index: int, gain: str = "auto", ppm: int = 0) -> list[str]:
    """AIS-catcher on device `index` (resolved from the exact serial just before), JSON_FULL lines on stdout."""
    return [binary_, f"-d:{index}", "-gr", "rtlagc", "on", "tuner", gain, "-p", str(int(ppm)), "-o", "5"]


# -- the dongle (adsbd.py's lookup; stdlib only here) -------------------------------------------------------------------

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
    """[(index, serial)] plugged in now; None when librtlsdr can't be loaded. (Reads USB strings: opens no dongle.)"""
    lib = _load()
    if lib is None:
        return None
    out = []
    for i in range(int(lib.rtlsdr_get_device_count())):
        m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
        if lib.rtlsdr_get_device_usb_strings(i, m, p, s) == 0:
            out.append((i, s.value.decode(errors="replace")))
    return out


# -- the shared claims (sizeup/radio/claims.py's format) ------------------------------------------------------------------


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
        pass
    return True


def release(serial: str, d: Path | None = None) -> None:
    c = holder(serial, d)
    if c and c.get("app") == APP and int(c.get("pid", -1)) == os.getpid():
        try:
            ((d or claims_dir()) / f"{serial}.json").unlink()
        except OSError:
            pass


# -- the messages -------------------------------------------------------------------------------------------------------


def group(m: dict) -> str | None:
    t = m.get("type")
    if t in POS_TYPES:
        return "pos"
    if t == 5:
        return "s5"
    if t == 24:
        return {0: "s24a", 1: "s24b"}.get(m.get("partno"))
    return None


class Keeper:
    """Each MMSI's latest message of each kind, trimmed, with when it was heard. Thread-safe."""

    def __init__(self):
        self.msgs: dict[tuple[int, str], dict] = {}
        self._lock = threading.Lock()
        self.heard: deque = deque()          # when each message was heard (the last minute)
        self.last: float | None = None

    def line(self, raw: str, t: float) -> bool:
        raw = raw.strip()
        if not raw.startswith("{"):
            return False
        try:
            m = json.loads(raw)
        except ValueError:
            return False
        return self.keep(m, t)

    def keep(self, m, t: float) -> bool:
        if not isinstance(m, dict) or not isinstance(m.get("mmsi"), int) or isinstance(m.get("mmsi"), bool):
            return False
        g = group(m)
        if g is None:
            return False
        rec = {k: m[k] for k in FIELDS if k in m}
        rec["t"] = round(t, 1)
        with self._lock:
            self.msgs[(m["mmsi"], g)] = rec
            self.heard.append(t)
            self.last = t
        return True

    def dump(self, now: float) -> list[dict]:
        with self._lock:
            for k in [k for k, r in self.msgs.items() if now - r["t"] > (POS_KEEP_S if k[1] == "pos" else STATIC_KEEP_S)]:
                del self.msgs[k]
            if len(self.msgs) > MAX_MSGS:
                for k in sorted(self.msgs, key=lambda k: self.msgs[k]["t"])[:len(self.msgs) - MAX_MSGS]:
                    del self.msgs[k]
            while self.heard and now - self.heard[0] > RATE_WINDOW_S:
                self.heard.popleft()
            return sorted(self.msgs.values(), key=lambda r: -r["t"])

    def stats(self) -> dict:
        with self._lock:
            return {"messages_per_s": round(len(self.heard) / RATE_WINDOW_S, 2),
                    "vessels": len({k[0] for k in self.msgs if k[1] == "pos"}), "last_message_at": self.last}


def write_json(path: str, doc) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}")
        tmp.write_text(json.dumps(doc, separators=(",", ":")))
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except OSError:
        pass


class Decoder:
    """One step every STEP_S: what the config wants, the dongle and its claim, AIS-catcher started / watched / stopped."""

    def __init__(self, config: str = CONFIG, messages: str = MESSAGES, status: str = STATUS,
                 find_binary=binary, find_devices=devices, popen=subprocess.Popen, claims: Path | None = None,
                 clock=time.time, log=lambda m: print(m, flush=True)):
        self.config, self.messages_path, self.status_path = config, messages, status
        self.find_binary, self.find_devices, self.popen, self.claims = find_binary, find_devices, popen, claims
        self.clock, self.log = clock, log
        self.keeper = Keeper()
        self.proc = None
        self.sdr = ""
        self.want: tuple = ()
        self.since: float | None = None
        self.restarts = 0
        self.fails = 0
        self.next_start = 0.0
        self.written = 0.0
        self.status: dict = {"state": "off", "detail": "", "at": None}

    def _set(self, now: float, state: str, detail: str, **kw) -> None:
        if (state, detail) != (self.status.get("state"), self.status.get("detail")):
            self.log(f"ais: {state}{': ' + detail if detail else ''}")
        self.status = {"at": now, "state": state, "detail": detail, "sdr": self.sdr,
                       "pid": self.proc.pid if self.proc is not None else None, "since": self.since,
                       "restarts": self.restarts, "held_by": None, **self.keeper.stats(), **kw}

    def stop(self, why: str = "") -> None:
        p, self.proc = self.proc, None
        if p is not None:
            if why:
                self.log(f"ais: stopping AIS-catcher ({why})")
            try:
                p.terminate()
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
            except OSError:
                pass
        if self.sdr:
            release(self.sdr, self.claims)
        self.since = None

    def _reader(self, proc) -> None:
        try:
            for raw in proc.stdout:
                self.keeper.line(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"), self.clock())
        except (OSError, ValueError):
            pass

    def step(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        s = settings(self.config)
        want = (s["enabled"], s["sdr"], s["gain"], s["ppm"])
        if want != self.want and self.proc is not None:
            self.stop("the settings changed")
        if want != self.want:
            self.fails, self.next_start = 0, 0.0
        self.want = want
        if self.proc is None and self.sdr and self.sdr != s["sdr"]:
            release(self.sdr, self.claims)
        self.sdr = s["sdr"]
        if not s["enabled"] or not s["sdr"] or s["problem"]:
            self._set(now, "off", s["problem"] or ("no dongle named ([vessels.receiver] sdr)" if s["enabled"]
                                                   else "[vessels.receiver] enabled = false"))
        elif self.proc is not None:
            self._watch(now)
        elif now >= self.next_start:
            self._start(now, s)
        if now - self.written >= WRITE_S:
            self.written = now
            write_json(self.messages_path, {"at": round(now, 1), "msgs": self.keeper.dump(now)})
        write_json(self.status_path, self.status)
        return self.status

    def _start(self, now: float, s: dict) -> None:
        exe = self.find_binary()
        if not exe:
            self._set(now, "missing", f"AIS-catcher not installed ({AIS_CATCHER}); deploy/station/README.md")
            return
        c = other(s["sdr"], self.claims)
        if c:
            self._set(now, "held", f"{s['sdr']} is in use by {c.get('app', 'another app')} ({c.get('mode', '?')})",
                      held_by={"app": c.get("app"), "mode": c.get("mode")})
            self.next_start = now + BACKOFF_S[0]
            return
        found = self.find_devices()
        hits = [i for i, ser in (found or []) if ser == s["sdr"]]
        if found is None:
            self._set(now, "missing", "librtlsdr can't be loaded")
            self.next_start = now + BACKOFF_S[1]
            return
        if not hits:
            self._set(now, "missing", f"{s['sdr']} isn't plugged in")
            self.next_start = now + BACKOFF_S[0] * 2
            return
        if len(hits) > 1:
            self._set(now, "ambiguous", f"{len(hits)} dongles share serial {s['sdr']}: rename one (rtl_eeprom -s)")
            self.next_start = now + BACKOFF_S[1]
            return
        if not claim(s["sdr"], self.claims):
            self.next_start = now + BACKOFF_S[0]
            return
        argv = ais_argv(exe, hits[0], s["gain"], s["ppm"])
        try:
            self.proc = self.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                   text=True, bufsize=1)
        except OSError as e:
            release(s["sdr"], self.claims)
            self._fail(now, f"couldn't start AIS-catcher: {e}")
            return
        self.since = now
        threading.Thread(target=self._reader, args=(self.proc,), name="ais-stdout", daemon=True).start()
        self._set(now, "starting", f"AIS-catcher on {s['sdr']} (index {hits[0]})", argv=argv)

    def _watch(self, now: float) -> None:
        code = self.proc.poll()
        if code is not None:
            self.proc = None
            release(self.sdr, self.claims)
            self.restarts += 1
            self._fail(now, f"AIS-catcher exited ({code})")
            return
        last = self.keeper.last
        if last is not None and now - last <= NO_SIGNAL_S:
            self._set(now, "ok", "")
        elif now - (self.since or now) > NO_SIGNAL_S:
            self._set(now, "no_signal", f"nothing heard for {NO_SIGNAL_S / 60:.0f} min (the antenna, or a quiet harbour)")
        else:
            self._set(now, "starting", "")

    def _fail(self, now: float, why: str) -> None:
        self.fails += 1
        wait = min(BACKOFF_S[1], BACKOFF_S[0] * 2 ** min(self.fails - 1, 10))
        self.next_start = now + wait
        self.since = None
        self._set(now, "down", f"{why}; again in {wait:.0f} s")


def read_status(path: str | None = None, now: float | None = None) -> dict | None:
    """The decoder's last status; one older than 30 s says the service isn't running."""
    try:
        st = json.loads(Path(path or STATUS).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None
    now = time.time() if now is None else now
    if isinstance(st.get("at"), (int, float)) and now - st["at"] > 30:
        st = dict(st, state="down", detail=f"the decoder service hasn't reported for {now - st['at']:.0f} s "
                                           "(systemctl status sizeup-ais)")
    return st


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "run"
    if cmd == "print":
        s = settings()
        if not s["enabled"] or not s["sdr"] or s["problem"]:
            print(s["problem"] or "[vessels.receiver] off or no dongle named: nothing runs")
            return 0
        hits = [i for i, ser in (devices() or []) if ser == s["sdr"]]
        idx = hits[0] if len(hits) == 1 else -1
        print(" ".join(ais_argv(binary() or AIS_CATCHER, idx, s["gain"], s["ppm"]))
              + ("" if idx >= 0 else f"   ({s['sdr']} isn't plugged in now, or two dongles share it)"))
        return 0
    if cmd == "status":
        st = read_status()
        print(json.dumps(st, indent=2) if st else "no status yet (is sizeup-ais running?)")
        return 0
    if cmd != "run":
        print("usage: aisd.py run | print | status", file=sys.stderr)
        return 2
    d = Decoder()
    stopping = []

    def bye(*_a):
        stopping.append(1)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    d.log(f"ais: decoder supervisor up; the dongle named in {CONFIG} [vessels.receiver] sdr")
    try:
        while not stopping:
            try:
                d.step()
            except Exception as e:  # noqa: BLE001 - supervision must never die
                d.log(f"ais: {type(e).__name__}: {e}")
            for _ in range(int(STEP_S * 10)):
                if stopping:
                    break
                time.sleep(0.1)
    finally:
        d.stop("the service is stopping")
        d.status.update(state="off", detail="the decoder service stopped", at=time.time())
        write_json(d.status_path, d.status)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
