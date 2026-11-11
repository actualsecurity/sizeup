"""The reception test (GitHub #410): can this station hear another P25 system with a dongle it already has?

A person lends ONE dongle to one side of a system's span (a job of sizeup/radio/systems.py: "MRRS 770" / "MRRS 773";
one 2.4 MHz dongle takes in one side at a time) for 15 min, 1 h, 4 h or until stopped, from Settings > Radios. The
system's own trunk-recorder runs on it as it would for good, and its output is measured instead of kept:

    Probe        one side's numbers, from trunk-recorder's lines and its finished calls:
                   the control channel: which frequency, the decode rate (messages / s, logged every 3 s with
                     "controlWarnRate": -1), the share of the time it decoded, the longest dropout, the retunes;
                   the site it decoded (system ID, WACN, NAC, RFSS, site) against the one listed;
                   the grants per talkgroup (the unit script's lines: every talkgroup, on either side of the span),
                     and how many of them were on a voice channel this dongle doesn't cover;
                   the voice calls it captured: calls / hour, seconds of audio, decode errors per second of audio,
                     the signal and noise trunk-recorder measured;
                   encrypted and unknown talkgroups: a count, nothing else.
    sweep()      before trunk-recorder takes the dongle: rtl_power over that side, the level of each listed frequency
                 over the noise floor (left out when rtl_power isn't there).
    verdict()    good / marginal / none, with a hint in plain words.
    Tests        the test itself: which dongle may be lent, the job it pauses and gets back, the history.

Nothing of a test is kept as traffic: no transcription, no radio log row, no activity row, no radio ID. The calls it
captured are counted and their audio kept as a rolling sample for the tester to listen to (Settings > Radios, behind
the sign-in): `keep_min` minutes (default 10, 0 = none), then deleted; all of it is deleted when sizeup starts.

The dongle: never one another app holds (sizeup/radio/claims.py), never the ADS-B receiver's, never one that runs a job
unless the request names that job ("pause": the job, exactly as it is now), and a fire job's dongle (the county trunk,
paging, a fireground) only with "protected": true on top. The job it had is written down BEFORE anything is switched
(state/reception_test.json), and goes back on the dongle when the test ends, is stopped, fails, or when sizeup starts
and finds a test that never ended (recover()).
"""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from sizeup.radio import claims
from sizeup.radio.live import ANSI
from sizeup.radio.recorder import CC_LOW, CC_RATE

OFF = "Off"
DURATIONS = (15, 60, 240, 0)          # minutes a side; 0: until stopped
UNTIL_STOPPED_MAX_S = 24 * 3600.0     # "until stopped" ends by itself: a forgotten test never keeps a dongle for good
KEEP_MIN_DEFAULT, KEEP_MIN_MAX = 10.0, 60.0
SAMPLE_MAX_S = 60.0                   # of one call
SAMPLES_MAX = 40
HISTORY_MAX = 30
NOTE_MAX = 200
SAVE_S = 30.0                         # the running test's numbers are written down this often (a restart keeps them)
START_GRACE_S = 45.0                  # trunk-recorder not running this long: the test failed (and the dongle's job is
                                      # back before the hub's "job not running" alert, health.py JOB_GRACE_S)
CC_STEP_S = 3.0                       # trunk-recorder logs the decode rate this often
CC_OK = 10.0                          # messages / s: at or over this a 3 s window counts as decoded (its own warn rate)
SERIES_MAX = 120                      # points of the decode rate over time
# The verdict (docs/reception-test.md)
GOOD_PCT = 90.0                       # decoded this share of the time, or more: good
NONE_PCT = 5.0                        # under this: none
VOICE_ERR_BAD = 5.0                   # decode errors per second of voice, over this: marginal whatever the control channel
SHORT_S = 120.0                       # a side measured for less: said in the hint
QUIET_S = 3600.0                      # no grant on a watched talkgroup this long into a good side: the LTE caveat
SWEEP_BIN_HZ = 6250
SWEEP_TIMEOUT_S = 30.0
SAMPLE_ID = re.compile(r"[0-9a-f]{12}")

CC_AT = re.compile(r"(?:Started with|Retuning to) Control Channel:\s*(?P<mhz>[\d.]+)\s*MHz", re.I)
CC_FREQ = re.compile(r"freq:\s*(?P<mhz>[\d.]+)\s*MHz\s+Control Channel Message Decode Rate", re.I)
SYS_ID = re.compile(r"Decoding System ID\s+(?P<sysid>[0-9A-F]+)\s+WACN:\s*(?P<wacn>[0-9A-F]+)\s+NAC:\s*(?P<nac>[0-9A-F]+)", re.I)
SYS_SITE = re.compile(r"Decoding System Site\s+RFSS:\s*(?P<rfss>\d+)\s+SITE ID:\s*(?P<site>\d+)", re.I)
GRANT = re.compile(r"SIZEUP-UNIT (?P<sys>\S+) -?\d+ call (?P<tg>\d+)")
UNCOVERED = re.compile(r"TG:\s*(?P<tg>\d+)\s+Freq:.*Not Recording: no source covering", re.I)


def _hex(v) -> int | None:
    try:
        return int(str(v).strip(), 16)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def site_check(seen: dict, listed: dict) -> dict:
    """What the control channel said it is against the site as listed (a system's "site" block): {"seen", "match":
    True / False / None (nothing decoded, or nothing listed to hold it against), "differs": the fields that differ}."""
    differs, checked = [], 0
    for k, read in (("nac", _hex), ("wacn", _hex), ("rfss", _int), ("site", _int)):
        a, b = read(seen.get(k)), read(listed.get(k))
        if a is None or b is None:
            continue
        checked += 1
        if a != b:
            differs.append(k)
    a = _hex(seen.get("sysid"))
    want = listed.get("sysid")
    want = [_hex(x) for x in (want if isinstance(want, (list, tuple)) else [want])] if want else []
    if a is not None and want:
        checked += 1
        if a not in want:
            differs.append("sysid")
    return {"seen": dict(seen), "match": (not differs) if checked else None, "differs": differs}


class Probe:
    """One side of a reception test. feed_line(each line trunk-recorder says), call(each finished call), result()."""

    def __init__(self, system, job: str, sdr: str = "", clock: Callable[[], float] = time.time, sweep: dict | None = None):
        self.system, self.job, self.sdr, self.clock = system, job, sdr, clock
        src = next((s for s in system.sources if s.job == job), None)
        self.center = src.center if src else 0.0
        self.covered = system.covered([src.index]) if src else []
        self.started = clock()
        self.ended: float | None = None
        self.sweep = sweep
        self.cc_mhz: float | None = None           # the control channel it is on now
        self.per_freq: dict[float, list[float]] = {}   # MHz -> [windows, sum of rates, decoded windows]
        self.windows = self.decoded = self.locked = 0
        self.rate_sum, self.rate_min, self.rate_max = 0.0, None, None
        self.retunes = 0
        self._low_run = self.dropout_s = 0.0
        self._buckets: dict[int, list[float]] = {}     # minute into the side -> [sum, n]
        self.seen: dict = {}
        self.tgs: dict[int, dict] = {}
        self.encrypted = self.unknown = 0
        self.calls = self.errors = 0
        self.audio_s = 0.0
        self._sig: list[float] = []
        self._snr: list[float] = []
        self.first_decode: float | None = None
        self._lock = threading.Lock()

    def _tg(self, tg: int) -> dict:
        return self.tgs.setdefault(tg, {"grants": 0, "uncovered": 0, "calls": 0, "audio_s": 0.0, "errors": 0})

    def feed_line(self, raw: str) -> None:
        with self._lock:
            self._feed(ANSI.sub("", raw))

    def _feed(self, line: str) -> None:
        if "SIZEUP-UNIT " in line:
            m = GRANT.search(line)
            if m and m["sys"] == self.system.name:
                tg = int(m["tg"])
                info = self.system.talkgroups.get(tg)
                if info is None:
                    self.unknown += 1
                elif info.get("encrypted"):
                    self.encrypted += 1          # a count: never which radio, never decoded
                else:
                    self._tg(tg)["grants"] += 1
            return
        if "ontrol" in line:
            m = CC_RATE.search(line)
            if m:
                at = CC_FREQ.search(line)
                if at:
                    self.cc_mhz = round(float(at["mhz"]), 5)
                try:
                    self._window(float(m["rate"]))
                except ValueError:
                    pass
                return
            m = CC_AT.search(line)
            if m:
                self.cc_mhz = round(float(m["mhz"]), 5)
                self.retunes += "Retuning" in line
                return
        if "Decoding System" in line:
            for rx in (SYS_ID, SYS_SITE):
                m = rx.search(line)
                if m:
                    self.seen.update({k: v.upper() if k in ("sysid", "wacn", "nac") else int(v)
                                      for k, v in m.groupdict().items()})
            return
        if "Not Recording" in line:
            m = UNCOVERED.search(line)
            if m and int(m["tg"]) in self.system.talkgroups and not self.system.talkgroups[int(m["tg"])].get("encrypted"):
                self._tg(int(m["tg"]))["uncovered"] += 1

    def _window(self, rate: float) -> None:
        now = self.clock()
        self.windows += 1
        self.rate_sum += rate
        self.rate_min = rate if self.rate_min is None else min(self.rate_min, rate)
        self.rate_max = rate if self.rate_max is None else max(self.rate_max, rate)
        ok = rate >= CC_OK
        self.decoded += ok
        self.locked += rate >= CC_LOW
        if ok:
            self._low_run = 0.0
            if self.first_decode is None:
                self.first_decode = now
        else:
            self._low_run += CC_STEP_S
            self.dropout_s = max(self.dropout_s, self._low_run)
        if self.cc_mhz is not None:
            f = self.per_freq.setdefault(self.cc_mhz, [0, 0.0, 0])
            f[0] += 1
            f[1] += rate
            f[2] += ok
        b = self._buckets.setdefault(int(max(0.0, now - self.started) // 60), [0.0, 0])
        b[0] += rate
        b[1] += 1

    def call(self, call) -> dict:
        """A finished call this dongle captured: counted (its row for the sample list is returned)."""
        with self._lock:
            return self._call(call)

    def _call(self, call) -> dict:
        tg = int(call.talkgroup)
        audio = getattr(call, "audio", None)
        secs = len(audio) / 16000.0 if audio is not None else max(0.0, float(call.end) - float(call.start))
        errs = sum(int(getattr(t, "errors", 0) or 0) for t in getattr(call, "turns", []) or [])
        row = self._tg(tg)
        row["calls"] += 1
        row["audio_s"] += secs
        row["errors"] += errs
        self.calls += 1
        self.audio_s += secs
        self.errors += errs
        sig, noise = getattr(call, "signal", None), getattr(call, "noise", None)
        if sig is not None and noise is not None:
            self._sig.append(float(sig))
            self._snr.append(float(sig) - float(noise))
        return {"tg": tg, "name": (self.system.talkgroups.get(tg) or {}).get("name") or f"TG {tg}",
                "at": round(float(call.start), 1), "seconds": round(secs, 1), "errors": errs}

    def seconds(self) -> float:
        return max(0.0, (self.ended or self.clock()) - self.started)

    def _series(self) -> tuple[list[float], int]:
        """The mean decode rate per step of the side, at most SERIES_MAX points: (points, seconds a point)."""
        if not self._buckets:
            return [], 60
        n = max(self._buckets) + 1
        per = max(1, -(-n // SERIES_MAX))
        out = []
        for k in range(0, n, per):
            s = c = 0.0
            for j in range(k, min(n, k + per)):
                b = self._buckets.get(j)
                if b:
                    s, c = s + b[0], c + b[1]
            out.append(round(s / c, 1) if c else 0.0)
        return out, per * 60

    def result(self) -> dict:
        with self._lock:
            return self._result()

    def _result(self) -> dict:
        secs = self.seconds()
        hours = secs / 3600.0
        expected = int(secs // CC_STEP_S)
        best = max(self.per_freq.items(), key=lambda kv: (kv[1][2], kv[1][1]), default=None)
        found = best is not None and best[1][2] > 0
        series, step = self._series()
        watch = [int(t) for t in self.system.reception.get("watch") or [] if str(t).isdigit() or isinstance(t, int)]
        tgs = []
        for tg in sorted(set(self.tgs) | set(watch), key=lambda t: (t not in watch, t)):
            r = self.tgs.get(tg) or {"grants": 0, "uncovered": 0, "calls": 0, "audio_s": 0.0, "errors": 0}
            tgs.append({"tg": tg, "name": (self.system.talkgroups.get(tg) or {}).get("name") or f"TG {tg}",
                        "watch": tg in watch, "grants": r["grants"], "uncovered": r["uncovered"], "calls": r["calls"],
                        "calls_per_h": round(r["calls"] / hours, 1) if hours > 0 else None,
                        "audio_s": round(r["audio_s"], 1),
                        "err_per_s": round(r["errors"] / r["audio_s"], 2) if r["audio_s"] > 0 else None})
        sweep = self.sweep or None
        cc_over = None
        if sweep and found:
            cc_over = next((x["over_db"] for x in sweep["channels"] if abs(x["mhz"] - best[0]) < 0.001), None)
        mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None   # noqa: E731
        out = {
            "job": self.job, "sdr": self.sdr, "mhz": round(self.center / 1e6, 5),
            "covers": [round(f / 1e6, 5) for f in self.covered],
            "started": round(self.started, 1), "ended": round(self.ended, 1) if self.ended else None,
            "seconds": round(secs, 1),
            "cc": {"found": found, "mhz": best[0] if found else None,
                   "rate": round(best[1][1] / best[1][0], 1) if found else (
                       round(self.rate_sum / self.windows, 1) if self.windows else None),
                   "decoded_pct": round(100.0 * self.decoded / self.windows, 1) if self.windows else 0.0,
                   "locked_pct": round(100.0 * self.locked / self.windows, 1) if self.windows else 0.0,
                   "windows": self.windows, "expected": expected,
                   "logged_pct": round(min(100.0, 100.0 * self.windows / expected), 1) if expected else None,
                   "min": self.rate_min, "max": self.rate_max, "retunes": self.retunes,
                   "dropout_s": round(self.dropout_s), "series": series, "series_step_s": step,
                   "lock_s": round(self.first_decode - self.started, 1) if self.first_decode else None,
                   "per_freq": [{"mhz": f, "windows": int(v[0]), "rate": round(v[1] / v[0], 1),
                                 "decoded_pct": round(100.0 * v[2] / v[0], 1)}
                                for f, v in sorted(self.per_freq.items())]},
            "site": site_check(self.seen, self.system.site),
            "signal": {"sweep": sweep, "cc_over_db": cc_over, "voice_db": mean(self._sig), "voice_snr_db": mean(self._snr)},
            "talkgroups": tgs,
            "encrypted_grants": self.encrypted, "unknown_grants": self.unknown,
            "voice": {"calls": self.calls, "audio_s": round(self.audio_s, 1),
                      "calls_per_h": round(self.calls / hours, 1) if hours > 0 else None,
                      "err_per_s": round(self.errors / self.audio_s, 2) if self.audio_s > 0 else None},
        }
        out["verdict"] = verdict(out, self.system)
        return out


def _side(job: str) -> str:
    return job.rsplit(" ", 1)[-1] if " " in job else job


def verdict(r: dict, system=None) -> dict:
    """{"grade": "good" | "marginal" | "none", "hint": one plain sentence or two, "short": measured under SHORT_S}.

    none      the control channel decoded (CC_OK messages / s or more in a 3 s window) under NONE_PCT % of the time
    good      GOOD_PCT % or more, the site as listed (or not said), and voice, if any, under VOICE_ERR_BAD errors / s
    marginal  the rest: decoded some of the time, another site's control channel, or voice that decodes with errors"""
    rec = dict(getattr(system, "reception", None) or {})
    antenna = str(rec.get("antenna") or "a better or directional antenna")
    caveat = str(rec.get("caveat") or "")
    cc, site, voice = r["cc"], r["site"], r["voice"]
    pct, side = cc["decoded_pct"], _side(r["job"])
    watched = [t for t in r["talkgroups"] if t["watch"]]
    names = " / ".join(str(t["tg"]) for t in watched)
    short = r["seconds"] < SHORT_S
    hints: list[str] = []
    if not cc["windows"]:
        grade = "none"
        hints.append("trunk-recorder logged no decode rate: it didn't run on this dongle")
    elif pct < NONE_PCT:
        grade = "none"
        hints.append(f"No control channel on the {side} side" + (f" (decoded {pct:g}% of the time)" if pct else "")
                     + f": try the other side, then {antenna}")
    elif pct < GOOD_PCT:
        grade = "marginal"
        hints.append(f"Control channel decodes {pct:g}% of the time: try {antenna}")
    else:
        grade = "good"
    if grade != "none" and site["match"] is False:
        grade = "marginal"
        want = getattr(system, "site", None) or {}
        hints.append("Not the listed site: " + ", ".join(
            f"{k.upper()} {site['seen'].get(k)} (listed {'/'.join(map(str, want[k])) if isinstance(want.get(k), list) else want.get(k)})"
            for k in site["differs"]))
    if grade != "none" and voice["err_per_s"] is not None and voice["err_per_s"] > VOICE_ERR_BAD:
        grade = "marginal"
        hints.append(f"Voice decodes with {voice['err_per_s']:g} errors/s: try {antenna}")
    if grade != "none" and watched:
        grants, calls = sum(t["grants"] for t in watched), sum(t["calls"] for t in watched)
        if grants and not calls:
            hints.append(f"{names}: {grants} grants, no voice on the {side} side (its voice channels are on the other "
                         "side: test it too)")
        elif not grants and not calls:
            if r["seconds"] >= QUIET_S:
                hints.append(f"No {names} grant in {r['seconds'] / 3600:.1f} h" + (f". {caveat}" if caveat else "")
                             + " Compare with a scanner over the same time")
            else:
                hints.append(f"No {names} grant yet: run 1 h or more")
    if cc["logged_pct"] is not None and cc["windows"] and cc["logged_pct"] < 50 and not short:
        hints.append(f"trunk-recorder ran {cc['logged_pct']:g}% of the test")
    if short:
        hints.append(f"Short test ({round(r['seconds'])} s)")
    return {"grade": grade, "hint": ". ".join(h.rstrip(".") for h in hints) + ("." if hints else ""), "short": short}


def summary(h: dict) -> dict:
    """One side of a past test as the history shows it (the file keeps all of it)."""
    cc, v = h.get("cc") or {}, h.get("verdict") or {}
    return {"job": h.get("job"), "seconds": h.get("seconds"), "grade": v.get("grade"), "hint": v.get("hint"),
            "cc_mhz": cc.get("mhz"), "rate": cc.get("rate"), "decoded_pct": cc.get("decoded_pct"),
            "dropout_s": cc.get("dropout_s"), "retunes": cc.get("retunes"), "site": (h.get("site") or {}).get("match"),
            "cc_over_db": (h.get("signal") or {}).get("cc_over_db"), "voice_snr_db": (h.get("signal") or {}).get("voice_snr_db"),
            "watch": [{k: t.get(k) for k in ("tg", "name", "grants", "calls", "calls_per_h", "err_per_s")}
                      for t in h.get("talkgroups") or [] if t.get("watch")],
            "calls": (h.get("voice") or {}).get("calls"), "err_per_s": (h.get("voice") or {}).get("err_per_s"),
            "encrypted_grants": h.get("encrypted_grants")}


def overall(halves: list[dict]) -> dict | None:
    """A test's verdict: its best side's (a system is heard if either side's control channel is)."""
    order = {"good": 2, "marginal": 1, "none": 0}
    done = [h for h in halves if h.get("verdict")]
    if not done:
        return None
    best = max(done, key=lambda h: (order.get(h["verdict"]["grade"], 0), h["cc"]["decoded_pct"]))
    return dict(best["verdict"], job=best["job"])


# -- the level of each listed frequency (rtl_power) ---------------------------------------------------------------------------


def parse_sweep(text: str, listed_hz, bin_hz: float = SWEEP_BIN_HZ) -> dict | None:
    """rtl_power's CSV ("date, time, Hz low, Hz high, Hz step, samples, dB, dB, ...", one row per hop) as
    {"floor_db": the median bin, "channels": [{"mhz", "db", "over_db"}]}: each listed frequency's strongest bin
    within a channel's width, and how far over the floor it is. None when there is nothing to read."""
    bins: list[tuple[float, float]] = []
    for row in text.splitlines():
        p = [x.strip() for x in row.split(",")]
        if len(p) < 7:
            continue
        try:
            low, step = float(p[2]), float(p[4])
            vals = [float(x) for x in p[6:] if x]
        except ValueError:
            continue
        bins.extend((low + k * step, v) for k, v in enumerate(vals) if v == v and abs(v) != float("inf"))
    if len(bins) < 8:
        return None
    floor = float(np.median([v for _f, v in bins]))
    chans = []
    for hz in listed_hz:
        near = [v for f, v in bins if abs(f - hz) <= bin_hz]
        if near:
            chans.append({"mhz": round(hz / 1e6, 5), "db": round(max(near), 1), "over_db": round(max(near) - floor, 1)})
    return {"floor_db": round(floor, 1), "channels": chans} if chans else None


def rtl_power_path(rtl_fm: str = "") -> str:
    """rtl_power beside the configured rtl_fm (the station's own rtl-sdr build), else on the PATH; "" for none."""
    if rtl_fm:
        p = Path(rtl_fm).expanduser().with_name("rtl_power")
        if p.is_file():
            return str(p)
        if "/" in rtl_fm:
            return ""
    return shutil.which("rtl_power") or ""


def sweep(binary: str, index: int | str, listed_hz, center: float, rate: float, gain: float = 40.0,
          run=subprocess.run) -> dict | None:
    """One rtl_power pass over a side of the span on dongle `index` (the caller holds its claim; nothing else of ours
    is on it). None when it can't be run or says nothing."""
    lo, hi = int(center - rate / 2), int(center + rate / 2)
    argv = [binary, "-d", str(index), "-f", f"{lo}:{hi}:{SWEEP_BIN_HZ}", "-g", f"{gain:g}", "-i", "2", "-1", "-"]
    try:
        p = run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=SWEEP_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_sweep((p.stdout or b"").decode(errors="replace"), listed_hz)


# -- the test ---------------------------------------------------------------------------------------------------------------


def _wav_8k(audio, max_s: float = SAMPLE_MAX_S) -> bytes:
    """A call's audio (float32, 16 kHz) as an 8 kHz WAV, its first max_s."""
    x = np.asarray(audio, dtype=np.float32)[:int(max_s * 16000):2]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes((np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class Tests:
    """The reception tests of one app. Everything a page asks goes through post() / state() / sample(); tick() runs it
    (a thread once start_thread() is called; the tests call it with their own clock)."""

    def __init__(self, app, state_dir: Path | None, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = time.time):
        self.app, self.log, self.clock = app, log, clock
        self.dir = Path(state_dir) if state_dir else None
        self.marker = self.dir / "reception_test.json" if self.dir else None
        self.history_file = self.dir / "reception_history.json" if self.dir else None
        self.samples_dir = self.dir / "reception_samples" if self.dir else None
        self.active: dict | None = None
        self.probe: Probe | None = None
        self.samples: list[dict] = []
        self.history: list[dict] = self._load_history()
        self.sweep_fn = sweep                      # (binary, index, listed, center, rate, gain) -> dict | None
        self.inline = False                        # the tests: the dongle is switched in the caller's thread
        self._lock = threading.RLock()
        self._saved = 0.0
        self._down_since: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- files --------------------------------------------------------------------------------------------------------

    def _load_history(self) -> list[dict]:
        try:
            rows = json.loads(self.history_file.read_text()) if self.history_file and self.history_file.is_file() else []
        except (OSError, ValueError):
            rows = []
        return [r for r in rows if isinstance(r, dict)][:HISTORY_MAX] if isinstance(rows, list) else []

    def _write(self, path: Path | None, doc) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")))
            tmp.replace(path)
        except OSError as e:
            self.log(f"reception test: {path.name} not written: {type(e).__name__}: {e}")

    def _save_active(self) -> None:
        t = self.active
        if t is None:
            return
        doc = dict(t)
        if self.probe is not None:
            doc["running"] = self.probe.result()
        self._write(self.marker, doc)
        self._saved = self.clock()

    def _to_history(self, t: dict) -> None:
        t = {k: v for k, v in t.items() if k != "running"}
        self.history.insert(0, t)
        del self.history[HISTORY_MAX:]
        self._write(self.history_file, self.history)

    # -- which dongle ---------------------------------------------------------------------------------------------------

    def protected(self, job: str) -> str:
        """Why this job's dongle isn't lent without a second confirmation ("" when it may be)."""
        app = self.app
        from sizeup.app import P25_MODE
        if job == P25_MODE:
            return "county fire dispatch (the P25 trunk)"
        kind = getattr(app, "_kind", {}).get(job)
        if kind == "paging":
            return "fire paging"
        if kind == "traffic":
            return "a fireground channel"
        return ""

    def dongles(self) -> list[dict]:
        """Each dongle with whether it can be lent now: {serial, name, job, present, held_by, protected, refuse}."""
        app = self.app
        sdr = app.sdr() if getattr(app, "shared", False) else None
        out = []
        for x in (sdr or {}).get("sdrs", []):
            job = x.get("mode") or OFF
            refuse = ""
            if not x.get("present"):
                refuse = "unplugged"
            elif x.get("held_by"):
                refuse = f"in use by {x['held_by'].get('app') or 'another app'}"
            elif x.get("ambiguous"):
                refuse = "serial shared"
            elif job in getattr(app, "_sys_jobs", {}):
                refuse = f"runs {job}"
            out.append({"serial": x["serial"], "name": x.get("name") or x["serial"], "job": job,
                        "present": bool(x.get("present")), "held_by": x.get("held_by"),
                        "protected": self.protected(job) if job != OFF else "", "refuse": refuse})
        return out

    def lent(self) -> tuple[str, str, str | None] | None:
        """(the dongle, the job it had, who set that) while a test has it: what App._save_modes writes for it, so a
        restart of any release starts the dongle on its own job, never on the test's."""
        t = self.active
        if t is None:
            return None
        cur = self.app.assign.get(t["sdr"], OFF)
        if cur in t["plan"] or (cur == OFF and t.get("step") == "starting"):
            return t["sdr"], t["was"], t.get("was_by")
        return None

    # -- what a page asks ---------------------------------------------------------------------------------------------

    def state(self) -> dict | None:
        """state()["reception"]: the systems a test can be run on, the dongles, the running test and its numbers so
        far, the sample list, the history. None when this app has no system to test."""
        app = self.app
        if not getattr(app, "systems", None):
            return None
        t, p, now = self.active, self.probe, self.clock()      # (no lock: a slow switch never holds the page up)
        run = None
        if t is not None:
            run = {k: t.get(k) for k in ("id", "system", "label", "sdr", "sdr_name", "was", "plan", "phase", "minutes",
                                         "started", "phase_started", "phase_until", "keep_min", "note")}
            run["halves"] = list(t.get("halves") or [])
            run["step"] = t.get("step") or "running"
            run["now"] = round(now, 1)
            run["running"] = p.result() if p is not None and run["step"] == "running" else None
        return {"systems": [{"name": rt.system.name, "label": rt.system.label,
                             "jobs": [{"job": s.job, "mhz": round(s.center / 1e6, 5)} for s in rt.system.sources],
                             "watch": list(rt.system.reception.get("watch") or []),
                             "caveat": str(rt.system.reception.get("caveat") or "")}
                            for rt in app.systems.values()],
                "durations": list(DURATIONS), "keep_min": KEEP_MIN_DEFAULT,
                "dongles": self.dongles(), "active": run,
                "samples": [dict(s, age_s=round(now - s["kept"])) for s in list(self.samples)],
                "history": [self._brief(h) for h in list(self.history)]}

    @staticmethod
    def _brief(h: dict) -> dict:
        out = {k: h.get(k) for k in ("id", "system", "label", "sdr", "sdr_name", "was", "plan", "minutes", "started",
                                     "ended", "status", "why", "note", "verdict", "restored")}
        out["halves"] = [summary(x) for x in h.get("halves") or [] if isinstance(x, dict)]
        return out

    def post(self, body: dict) -> str | None:
        """POST /api/reception: {"action": "start" | "stop" | "note", ...}. None, or why not."""
        if not isinstance(body, dict):
            return "a JSON object"
        action = body.get("action")
        if action == "start":
            return self.start(body)
        if action == "stop":
            return self.stop()
        if action == "note":
            return self.note(str(body.get("id") or ""), body.get("note"))
        return "action: start, stop or note"

    def note(self, tid: str, note) -> str | None:
        note = str(note or "").strip()[:NOTE_MAX]
        with self._lock:
            if self.active is not None and self.active["id"] == tid:
                self.active["note"] = note
                self._save_active()
                return None
            for h in self.history:
                if h.get("id") == tid:
                    h["note"] = note
                    self._write(self.history_file, self.history)
                    return None
        return "no such test"

    def start(self, o: dict) -> str | None:
        app = self.app
        with self._lock:
            if self.active is not None:
                return "a reception test is already running: stop it first"
            if not getattr(app, "shared", False):
                return "each channel has its own dongle here: nothing to lend"
            rt = app.systems.get(str(o.get("system") or "")) or (
                next(iter(app.systems.values())) if len(app.systems) == 1 and not o.get("system") else None)
            if rt is None:
                return f"no such system; one of {', '.join(app.systems) or 'none'}"
            label = rt.system.label
            jobs = o.get("jobs") if isinstance(o.get("jobs"), list) else [o.get("job")]
            plan = [j for j in jobs if isinstance(j, str)]
            if not plan or len(set(plan)) != len(plan) or any(j not in rt.system.jobs for j in plan):
                return f"jobs: one or more of {', '.join(rt.system.jobs)}, each once"
            try:
                minutes = int(o.get("minutes", DURATIONS[0]))
                keep = float(o.get("keep_min", KEEP_MIN_DEFAULT))
            except (TypeError, ValueError):
                return "minutes and keep_min: numbers"
            if minutes not in DURATIONS:
                return f"minutes: one of {', '.join(map(str, DURATIONS))} (0: until stopped)"
            if minutes == 0 and len(plan) > 1:
                return "until stopped: one side at a time"
            if not 0 <= keep <= KEEP_MIN_MAX:
                return f"keep_min: 0 to {KEEP_MIN_MAX:g}"
            running = [m for m in app.assign.values() if m in rt.system.jobs]
            if running:
                return f"{label} already runs on a dongle ({', '.join(running)}): its numbers are in Settings > Radios"
            sdr = str(o.get("sdr") or "")
            row = next((d for d in self.dongles() if d["serial"] == sdr), None)
            if row is None:
                return f"no SDR {sdr!r}"
            name = row["name"]
            held = claims.other(sdr)
            if held or row["held_by"]:
                h = held or row["held_by"]
                return (f"SDR {name} is in use by {h.get('app') or 'another app'} ({h.get('mode') or '?'}): switch it "
                        "Off there to hand it over")
            if row["refuse"]:
                return f"SDR {name}: {row['refuse']}"
            if getattr(app, "adsb_sdr", "") == sdr:
                return f"SDR {name} is reserved for the ADS-B receiver"
            was = row["job"]
            if was != OFF:
                if o.get("pause") != was:
                    return (f"SDR {name} runs {was}: it is paused for the test and comes back after. Confirm with "
                            f"\"pause\": \"{was}\"")
                why = self.protected(was)
                if why and o.get("protected") is not True:
                    return (f"SDR {name} runs {was}, {why}: NOT heard while the test runs. Not lent unless confirmed "
                            "with \"protected\": true")
            now = self.clock()
            t = {"id": uuid.uuid4().hex[:12], "system": rt.system.name, "label": label, "sdr": sdr, "sdr_name": name,
                 "was": was, "was_by": app.set_by.get(sdr), "plan": plan, "phase": 0, "minutes": minutes,
                 "keep_min": keep, "note": str(o.get("note") or "").strip()[:NOTE_MAX], "started": round(now, 1),
                 "phase_started": None, "phase_until": None, "halves": [], "status": "running", "step": "starting"}
            self.active = t
            self._save_active()                    # the job to give back is on disk before anything is switched
            self.log(f"reception test {t['id']}: {label} {' then '.join(plan)} on SDR {name}, "
                     + (f"{minutes} min a side" if minutes else "until stopped")
                     + (f"; {was} paused" if was != OFF else ""))
        if self.inline:
            self._begin()
        else:
            threading.Thread(target=self._guarded, args=(self._begin,), name="reception-test", daemon=True).start()
        return None

    def stop(self) -> str | None:
        with self._lock:
            if self.active is None:
                return "no reception test is running"
            self._finish("stopped", "stopped by a person")
        return None

    # -- running it -----------------------------------------------------------------------------------------------------

    def _guarded(self, fn) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - a test never leaves a dongle off its job
            self.log(f"reception test: {type(e).__name__}: {e}")
            with self._lock:
                if self.active is not None:
                    self._finish("failed", f"{type(e).__name__}: {e}")

    def _begin(self) -> None:
        """This side of the plan onto the dongle: the job it had off it (the first side), the sweep, then the
        system's trunk-recorder with a fresh Probe."""
        with self._lock:
            t = self.active
            if t is None:
                return
            app, sdr, job = self.app, t["sdr"], t["plan"][t["phase"]]
            rt = app.systems[t["system"]]
            t["step"] = "starting"
            got = None
            binary = rtl_power_path(getattr(app.cfg, "rtl_fm", "") or "")
            src = next(s for s in rt.system.sources if s.job == job)
            if binary:
                err = app.set_mode(OFF, sdr, by="person")
                if err:
                    return self._finish("failed", err)
                if claims.claim(sdr, f"{t['label']} test"):
                    try:
                        from sizeup.radio import sdr as sdr_lookup
                        idx, _why = sdr_lookup.lookup(sdr, app.cfg.rtl_fm)
                        if idx is not None:
                            got = self.sweep_fn(binary, idx, rt.system.covered([src.index]), src.center, src.rate)
                    except Exception as e:  # noqa: BLE001 - the level is an extra: the test goes on without it
                        self.log(f"reception test {t['id']}: sweep failed: {type(e).__name__}: {e}")
                    finally:
                        claims.release(sdr)
            self.probe = rt.probe = Probe(rt.system, job, sdr, self.clock, got)
            err = app.set_mode(job, sdr, by="person")
            if err:
                return self._finish("failed", err)
            now = self.clock()
            self.probe.started = now
            t["phase_started"] = round(now, 1)
            t["phase_until"] = round(now + (t["minutes"] * 60 if t["minutes"] else UNTIL_STOPPED_MAX_S), 1)
            t["step"] = "running"
            self._down_since = None
            self._save_active()

    def _end_side(self) -> None:
        t, p = self.active, self.probe
        if t is None or p is None:
            return
        p.ended = self.clock()
        t["halves"].append(p.result())
        rt = self.app.systems.get(t["system"])
        if rt is not None and rt.probe is p:
            rt.probe = None
        self.probe = None

    def _finish(self, status: str, why: str = "") -> None:
        """The test is over (done, stopped, failed): its numbers to the history, the dongle back on its job."""
        t = self.active
        if t is None:
            return
        self._end_side()
        t["restored"] = self._restore(t)
        t.update(status=status, why=why, ended=round(self.clock(), 1), step="ended", verdict=overall(t["halves"]))
        self.active = None
        self.app._save_modes()
        self._to_history(t)
        try:
            if self.marker is not None:
                self.marker.unlink(missing_ok=True)
        except OSError:
            pass
        v = t["verdict"]
        self.log(f"reception test {t['id']}: {status}" + (f" ({why})" if why else "")
                 + (f": {v['grade']}: {v['hint']}" if v else "") + f"; SDR {t['sdr_name']}: {t['restored']}")

    def _restore(self, t: dict) -> str:
        """The dongle back on the job it had. What was done, in words (kept with the test)."""
        app, sdr, was = self.app, t["sdr"], t["was"]
        rt = app.systems.get(t["system"])
        if rt is not None:
            rt.probe = None
        with app._mode_lock:
            cur = app.assign.get(sdr, OFF)
            if cur not in t["plan"] and cur != was and not (cur == OFF and t.get("step") == "starting"):
                return f"left on {cur} (changed during the test)"
            elsewhere = was != OFF and any(o != sdr and m == was for o, m in app.assign.items())
            target = OFF if elsewhere else was
            err = app.set_mode(target, sdr, by=t.get("was_by") or "auto")
            if err:
                # it can't run now (unplugged, taken): its job is still written down for it, as before the test
                try:
                    app._stop_mode(cur, "the reception test ended", sdr)
                except Exception as e:  # noqa: BLE001
                    self.log(f"reception test {t['id']}: {cur} not stopped: {type(e).__name__}: {e}")
                app.assign[sdr] = target
            if t.get("was_by"):
                app.set_by[sdr] = t["was_by"]
            else:
                app.set_by.pop(sdr, None)
            app._save_modes()
            if err:
                return f"{target} set for it, not started: {err}"
            return f"{was} now on another dongle: Off" if elsewhere else (f"{was} restored" if was != OFF else "Off again")

    def recover(self) -> None:
        """sizeup starts: a test that never ended (a restart, a crash, a power cut) is closed as interrupted and its
        dongle's job set back before the dongles are started; every sample is deleted."""
        self._wipe_samples()
        if self.marker is None or not self.marker.is_file():
            return
        try:
            t = json.loads(self.marker.read_text())
        except (OSError, ValueError):
            t = None
        app = self.app
        if isinstance(t, dict) and isinstance(t.get("sdr"), str) and isinstance(t.get("plan"), list):
            sdr, was = t["sdr"], str(t.get("was") or OFF)
            was = was if was in app.modes else OFF
            cur = app.assign.get(sdr, OFF)
            if cur in t["plan"] or cur == was or (cur == OFF and t.get("step") == "starting"):
                rt = app.systems.get(str(t.get("system")))
                if rt is not None and getattr(rt.recorder, "unit", False):
                    # its trunk-recorder service may still run the test's side: off, and the dongle let go, first
                    rt.recorder.stop("the reception test was ended by a restart")
                elsewhere = was != OFF and any(o != sdr and m == was for o, m in app.assign.items())
                app.assign[sdr] = OFF if elsewhere else was
                if t.get("was_by") in ("person", "auto") and not elsewhere:
                    app.set_by[sdr] = t["was_by"]
                else:
                    app.set_by.pop(sdr, None)
                app._save_modes()
                t["restored"] = f"{app.assign[sdr]} set back at the restart"
            else:
                t["restored"] = f"left on {cur} (changed during the test)"
            if isinstance(t.get("running"), dict):
                t.setdefault("halves", []).append(t["running"])
            t.update(status="interrupted", why="sizeup restarted during the test", step="ended",
                     ended=round(self.clock(), 1), verdict=overall(t.get("halves") or []))
            self._to_history(t)
            self.log(f"reception test {t.get('id')}: interrupted by a restart; SDR {t.get('sdr_name') or sdr}: "
                     f"{t['restored']}")
        try:
            self.marker.unlink(missing_ok=True)
        except OSError:
            pass

    def tick(self) -> None:
        """Once a second: a side that is over, a test whose trunk-recorder never ran, the samples that are too old."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            t = self.active
            if t is None or t.get("step") != "running" or self.probe is None:
                return
            app = self.app
            rt = app.systems[t["system"]]
            if app.assign.get(t["sdr"]) != t["plan"][t["phase"]]:
                return self._finish("failed", f"SDR {t['sdr_name']} was given another job during the test")
            state = rt.recorder.status().get("state")
            if state == "ok":
                self._down_since = None
            else:
                self._down_since = self._down_since or now
                if now - self._down_since >= START_GRACE_S:
                    return self._finish("failed", f"trunk-recorder {state}: {rt.recorder.status().get('detail') or ''}"[:200])
            if now >= (t["phase_until"] or 0):
                if t["phase"] + 1 < len(t["plan"]):
                    self._end_side()
                    t["phase"] += 1
                    t["step"] = "starting"
                    self._save_active()
                    return self._begin() if self.inline else threading.Thread(
                        target=self._guarded, args=(self._begin,), name="reception-test", daemon=True).start()
                return self._finish("done")
            if now - self._saved >= SAVE_S:
                self._save_active()

    def start_thread(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="reception-tests", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            self._guarded(self.tick)

    def shutdown(self) -> None:
        """sizeup is stopping. The test stays written down: the next start gives the dongle its job back (recover)."""
        self._stop.set()
        with self._lock:
            if self.active is not None:
                self._save_active()
        self._wipe_samples()

    # -- the calls it captured ------------------------------------------------------------------------------------------

    def on_call(self, rt, call) -> bool:
        """A finished call of a system: True when a test took it (counted; a sample kept), so nothing else may."""
        with self._lock:
            p = rt.probe
            if p is None:
                return False
            row = p.call(call)
            t = self.active
            audio = getattr(call, "audio", None)
            if t is not None and t.get("keep_min") and audio is not None and len(audio) and self.samples_dir is not None:
                sid = uuid.uuid4().hex[:12]
                try:
                    self.samples_dir.mkdir(parents=True, exist_ok=True)
                    (self.samples_dir / f"{sid}.wav").write_bytes(_wav_8k(audio))
                    self.samples.insert(0, dict(row, id=sid, kept=self.clock(), keep_s=float(t["keep_min"]) * 60, job=p.job))
                except OSError as e:
                    self.log(f"reception test: sample not kept: {type(e).__name__}: {e}")
                self._prune(self.clock())
            return True

    def _prune(self, now: float) -> None:
        keep = [s for s in self.samples if now - s["kept"] < s["keep_s"]][:SAMPLES_MAX]
        for s in self.samples:
            if s not in keep and self.samples_dir is not None:
                try:
                    (self.samples_dir / f"{s['id']}.wav").unlink(missing_ok=True)
                except OSError:
                    pass
        self.samples = keep

    def _wipe_samples(self) -> None:
        self.samples = []
        if self.samples_dir is not None and self.samples_dir.is_dir():
            shutil.rmtree(self.samples_dir, ignore_errors=True)

    def sample(self, sid: str) -> bytes | None:
        """A sample's WAV while it is kept."""
        with self._lock:
            self._prune(self.clock())
            if not SAMPLE_ID.fullmatch(str(sid)) or self.samples_dir is None or not any(s["id"] == sid for s in self.samples):
                return None
            try:
                return (self.samples_dir / f"{sid}.wav").read_bytes()
            except OSError:
                return None

    def sample_b64(self, sid: str) -> str:
        """The same for the hub (a station job's answer is JSON): "" for none."""
        wav = self.sample(sid)
        return base64.b64encode(wav).decode() if wav else ""
