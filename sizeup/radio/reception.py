"""The reception test (GitHub #410, #436): can this station hear a P25 system with a dongle it already has?

A person lends ONE dongle to one side of a system's span (a job of sizeup/radio/systems.py: "MRRS 770" / "MRRS 773";
one 2.4 MHz dongle takes in one side at a time; or the county's own site, a probe-only system of
sizeup/radio/probes.py: "County probe") for 15 min, 1 h, 4 h or until stopped, from the hub's Settings > Radios >
Receivers. The system's own trunk-recorder runs on it as it would for good (the county's: the probe's, never the
county's own), and its output is measured instead of kept:

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
captured are counted and their audio kept as a rolling sample for the tester to listen to: `keep_min` minutes
(default 10, 0 = none), then deleted; all of it is deleted when sizeup starts. The sample is another system's audio,
so it is listened to from the hub only (Settings > Radios there, a signed-in admin: the hub asks the station for the
call through its job queue, App.reception_sample_b64, and stores nothing). The station's own page, an all-in-one
install and a hub without Access serve neither the audio nor the list of captured calls (sizeup/web/server.py,
sourcekeys.may_see_context, #419): only how many are kept.

The dongle: never one another app holds (sizeup/radio/claims.py), never the ADS-B receiver's, never a fire job's (the
county trunk, paging, a fireground: not lendable at all in this version), and one that runs any other job only when
the request names that job ("pause": the job, exactly as it is now). The job it had is written down BEFORE anything
is switched (state/reception_test.json) and comes back when the test ends, is stopped (also: the lent dongle switched
in Settings > Radios), fails, or sizeup stops (shutdown()); after a crash or a power cut, when sizeup starts
(recover()), and the station's trunk-recorder service lets the dongle go by itself at the test's deadline ("until" in
the system's state file, sizeup/radio/trunkrecd.py). A job that can't be given back is owed and tried again until it
is; the station's health says so.
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
from sizeup.radio.systems import Runtime

OFF = "Off"
DURATIONS = (15, 60, 240, 0)          # minutes a side; 0: until stopped
UNTIL_STOPPED_MAX_S = 24 * 3600.0     # "until stopped" ends by itself: a forgotten test never keeps a dongle for good
KEEP_MIN_DEFAULT, KEEP_MIN_MAX = 10.0, 60.0
SAMPLE_MAX_S = 60.0                   # of one call
SAMPLES_MAX = 40
HISTORY_MAX = 30
NOTE_MAX = 200
SAVE_S = 30.0                         # the running test's numbers are written down this often (a restart keeps them)
START_STUCK_S = 180.0                 # a start (the sweep, the hand-off) that never finished: the test failed
OWED_RETRY_S = 5.0                    # a paused job that couldn't be given back is tried again this often
OVERDUE_S = 120.0                     # a test still running this long after its end: said in the station's health
LATE_CALL_S = 120.0                   # a call that started before a test's end + this is the test's: never traffic
UNTIL_GRACE_S = 120.0                 # the trunk-recorder service stops a test's system this long after the side's end
RECOVER_TRIES = 2                     # at a start after a crash: its trunk-recorder told to stop, the claim looked at
START_GRACE_S = 45.0                  # trunk-recorder not running this long: the test failed (and the dongle's job is
                                      # back before the hub's "job not running" alert, health.py JOB_GRACE_S)
CC_STEP_S = 3.0                       # trunk-recorder logs the decode rate this often
CC_OK = 10.0                          # messages / s: at or over this a 3 s window counts as decoded (its own warn rate)
SERIES_MAX = 120                      # points of the decode rate over time
# The verdict (docs/reception-test.md)
GOOD_PCT = 90.0                       # decoded this share of the time, or more: good ...
GOOD_RATE = 30.0                      # ... at this mean rate or more (messages / s; full strength is about 40).
                                      # UNCALIBRATED: a first guess, to be set against a scanner on the first real runs
NONE_PCT = 5.0                        # under this: none
VOICE_ERR_BAD = 5.0                   # decode errors per second of voice, over this: marginal whatever the control channel
SHORT_S = 120.0                       # a side measured for less: said in the hint
DOWN_PCT = 25                         # trunk-recorder not running this share of a side: said, instead of "not logged"
QUIET_S = 3600.0                      # no grant on a watched talkgroup this long into a good side: the LTE caveat
SWEEP_BIN_HZ = 6250
SWEEP_TIMEOUT_S = 30.0
SAMPLE_ID = re.compile(r"[0-9a-f]{12}")
PROBE_CLEAN_S = 60.0                  # (#436) a probe's capture directory is capped this often ...
PROBE_KEEP_S = 600.0                  # ... its calls older than this removed (a test takes them within seconds) ...
PROBE_MAX_BYTES = 200 * 1024 * 1024   # ... and the oldest over this (janitor.SYSTEM_MAX_BYTES)

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

    def __init__(self, system, job: str, sdr: str = "", clock: Callable[[], float] = time.time, sweep: dict | None = None,
                 alert=None):
        self.system, self.job, self.sdr, self.clock = system, job, sdr, clock
        # the talkgroups "TG hits/h" counts the grants of (#436): the system's alert tier, the county's fire ones
        self.alert = sorted({int(t) for t in (alert_tgs(system) if alert is None else alert)})
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
        self.down_s, self._ran_at = 0.0, None      # how long its trunk-recorder wasn't running (ran(), each tick)
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

    def ran(self, now: float, ok: bool) -> None:
        """Each tick of the test: whether the system's trunk-recorder runs now."""
        with self._lock:
            if self._ran_at is not None and not ok:
                self.down_s += max(0.0, now - self._ran_at)
            self._ran_at = now

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
        hits = sum((self.tgs.get(tg) or {}).get("grants", 0) for tg in self.alert)
        out = {
            "job": self.job, "sdr": self.sdr, "mhz": round(self.center / 1e6, 5),
            "covers": [round(f / 1e6, 5) for f in self.covered],
            "started": round(self.started, 1), "ended": round(self.ended, 1) if self.ended else None,
            "seconds": round(secs, 1),
            "cc": {"found": found, "mhz": best[0] if found else None,
                   "rate": round(best[1][1] / best[1][0], 1) if found else (
                       round(self.rate_sum / self.windows, 1) if self.windows else None),
                   # of the windows there should have been: one trunk-recorder didn't log is not a decoded one
                   "decoded_pct": round(100.0 * self.decoded / max(expected, self.windows), 1) if self.windows else 0.0,
                   "locked_pct": round(100.0 * self.locked / max(expected, self.windows), 1) if self.windows else 0.0,
                   "windows": self.windows, "expected": expected,
                   "logged_pct": round(min(100.0, 100.0 * self.windows / expected), 1) if expected else None,
                   "down_pct": round(min(100.0, 100.0 * self.down_s / secs)) if secs > 0 else 0,
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
            "alert": {"tgs": [{"tg": tg, "name": (self.system.talkgroups.get(tg) or {}).get("name") or f"TG {tg}"}
                              for tg in self.alert],
                      "grants": hits, "per_h": round(hits / hours, 1) if hours > 0 and self.alert else None},
            "voice": {"calls": self.calls, "audio_s": round(self.audio_s, 1),
                      "calls_per_h": round(self.calls / hours, 1) if hours > 0 else None,
                      "err_per_s": round(self.errors / self.audio_s, 2) if self.audio_s > 0 else None},
        }
        out["verdict"] = verdict(out, self.system)
        return out


def alert_tgs(system, tier=None) -> list[int]:
    """The talkgroups a test's "TG hits/h" is of (#436): the system's own list ("reception"."alert": the county's fire
    alert talkgroups), else its talkgroups of the alert tier (tier(tg): as set now; default: as in its file).
    Never an encrypted one (its grants are only counted)."""
    listed = (getattr(system, "reception", None) or {}).get("alert")
    if listed:
        return sorted(int(t) for t in listed if str(t).isdigit() or isinstance(t, int))
    from sizeup import tiers
    out = []
    for tg, i in (getattr(system, "talkgroups", None) or {}).items():
        t = tier(tg) if tier is not None else i.get("tier")
        if t == tiers.ALERT and not i.get("encrypted"):
            out.append(int(tg))
    return sorted(out)


def _side(job: str) -> str:
    return job.rsplit(" ", 1)[-1] if " " in job else job


def verdict(r: dict, system=None) -> dict:
    """{"grade": "good" | "marginal" | "none", "hint": one plain sentence or two, "short": measured under SHORT_S}.

    none      the control channel decoded (CC_OK messages / s or more in a 3 s window) under NONE_PCT % of the time
    good      GOOD_PCT % or more at a mean of GOOD_RATE messages / s or more, the site as listed (or not said), and
              voice, if any, under VOICE_ERR_BAD errors / s
    marginal  the rest: decoded some of the time or weakly, another site's control channel, voice with errors
    The share is of the windows there should have been (one every CC_STEP_S). When trunk-recorder logged the rate for
    under half of them (it only does with "controlWarnRate": -1) the rate can't be judged: "decode rate not logged",
    graded from what else shows the control channel was decoded: the site and the grants (both: good; one: marginal;
    never none)."""
    rec = dict(getattr(system, "reception", None) or {})
    antenna = str(rec.get("antenna") or "a better or directional antenna")
    caveat = str(rec.get("caveat") or "")
    cc, site, voice = r["cc"], r["site"], r["voice"]
    pct, side = cc["decoded_pct"], _side(r["job"])
    watched = [t for t in r["talkgroups"] if t["watch"]]
    names = " / ".join(str(t["tg"]) for t in watched)
    short = r["seconds"] < SHORT_S
    all_grants = sum(t["grants"] for t in r["talkgroups"]) + r["encrypted_grants"] + r["unknown_grants"]
    logged = cc["windows"] > 0 and cc["windows"] >= 0.5 * cc["expected"]
    hints: list[str] = []
    down = cc.get("down_pct") or 0
    if not logged and (site["seen"] or all_grants):
        grade = "good" if site["match"] is True and all_grants else "marginal"
        hints.append((f"trunk-recorder wasn't running for {down}% of the test" if down >= DOWN_PCT else
                      "Decode rate not logged" + (f" ({cc['logged_pct']:g}% of the time)" if cc["windows"] else ""))
                     + ": graded from the site and the grants seen")
    elif not logged and down >= DOWN_PCT:
        grade = "none"
        hints.append(f"trunk-recorder wasn't running for {down}% of the test: nothing to judge")
    elif not logged:
        grade = "none"
        hints.append(f"No control channel on the {side} side (no decode rate, no site, no grant): try the other side, "
                     f"then {antenna}" if cc["windows"] else
                     "trunk-recorder logged no decode rate, no site and no grant: it didn't run on this dongle")
    elif pct < NONE_PCT:
        grade = "none"
        hints.append(f"No control channel on the {side} side" + (f" (decoded {pct:g}% of the time)" if pct else "")
                     + f": try the other side, then {antenna}")
    elif pct < GOOD_PCT:
        grade = "marginal"
        hints.append(f"Control channel decodes {pct:g}% of the time: try {antenna}")
    elif cc["rate"] is not None and cc["rate"] < GOOD_RATE:
        grade = "marginal"
        hints.append(f"Control channel decodes at {cc['rate']:g}/s (full strength is about 40): try {antenna}")
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
    if short:
        hints.append(f"Short test ({round(r['seconds'])} s)")
    return {"grade": grade, "hint": ". ".join(h.rstrip(".") for h in hints) + ("." if hints else ""), "short": short}


def ended_text(t: dict) -> str:
    """A finished test in one line, for the activity log: how it ended, the verdict, what became of the dongle."""
    v = t.get("verdict") or {}
    return (f"Reception test {t.get('status') or 'ended'}"
            + (f" ({t['why']})" if t.get("why") and t.get("status") != "done" else "")
            + f": {' + '.join(map(str, t.get('plan') or []))} on SDR {t.get('sdr_name') or t.get('sdr')}"
            + (f" · {v['grade']}" if v.get("grade") else "") + f" · {t.get('restored') or 'NOT restored'}")[:300]


def restored_ok(t: dict) -> bool:
    return bool(t.get("restored_ok"))


def summary(h: dict) -> dict:
    """One side of a past test as the history shows it (the file keeps all of it)."""
    cc, v = h.get("cc") or {}, h.get("verdict") or {}
    return {"job": h.get("job"), "seconds": h.get("seconds"), "grade": v.get("grade"), "hint": v.get("hint"),
            "cc_mhz": cc.get("mhz"), "rate": cc.get("rate"),
            "decoded_pct": cc.get("decoded_pct") if cc.get("windows") else None,
            "dropout_s": cc.get("dropout_s"), "retunes": cc.get("retunes"), "site": (h.get("site") or {}).get("match"),
            "cc_over_db": (h.get("signal") or {}).get("cc_over_db"), "voice_snr_db": (h.get("signal") or {}).get("voice_snr_db"),
            "watch": [{k: t.get(k) for k in ("tg", "name", "grants", "calls", "calls_per_h", "err_per_s")}
                      for t in h.get("talkgroups") or [] if t.get("watch")],
            "calls": (h.get("voice") or {}).get("calls"), "err_per_s": (h.get("voice") or {}).get("err_per_s"),
            "encrypted_grants": h.get("encrypted_grants"), "windows": cc.get("windows"),
            "alert": h.get("alert") if isinstance(h.get("alert"), dict) else None}


def measured(h: dict) -> bool:
    """A side that measured anything: a decode rate logged, a site or a grant seen. One that didn't, of a test that
    was stopped, failed or interrupted, has no verdict (#436): "None" is a finished side that heard nothing."""
    cc = h.get("cc") or {}
    grants = sum(int(t.get("grants") or 0) for t in h.get("talkgroups") or [])
    return bool(cc.get("windows") or (h.get("site") or {}).get("seen") or grants or h.get("encrypted_grants")
                or h.get("unknown_grants"))


def unjudged(halves: list[dict], status: str) -> list[dict]:
    """A test that didn't run its course: the sides that measured nothing lose their verdict."""
    if status != "done":
        for h in halves:
            if isinstance(h, dict) and not measured(h):
                h["verdict"] = None
    return halves


def overall(halves: list[dict]) -> dict | None:
    """A test's verdict: its best side's (a system is heard if either side's control channel is)."""
    order = {"good": 2, "marginal": 1, "none": 0}
    done = [h for h in halves if isinstance(h, dict) and h.get("verdict")]
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
          popen=subprocess.Popen, started: Callable = lambda p: None) -> dict | None:
    """One rtl_power pass over a side of the span on dongle `index` (the caller holds its claim; nothing else of ours
    is on it). started(process): so a Stop can kill it. None when it can't be run, is killed or says nothing."""
    lo, hi = int(center - rate / 2), int(center + rate / 2)
    argv = [binary, "-d", str(index), "-f", f"{lo}:{hi}:{SWEEP_BIN_HZ}", "-g", f"{gain:g}", "-i", "2", "-1", "-"]
    try:
        p = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    started(p)
    try:
        out, _ = p.communicate(timeout=SWEEP_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        try:
            p.kill()
            p.communicate(timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass
        return None
    return parse_sweep((out or b"").decode(errors="replace"), listed_hz)


# -- the test ---------------------------------------------------------------------------------------------------------------


class ProbeRuntime(Runtime):
    """A probe-only system as the reception test runs it (#436, sizeup/radio/probes.py: the county's): its own
    trunk-recorder and capture directory, never a dongle job, never traffic. Its lines are a test's or nothing."""

    def _line(self, line: str) -> None:
        probe = self.probe
        if probe is not None:
            probe.feed_line(line)


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
    """The reception tests of one app. A page asks through post() / state() / sample(); tick() runs it once a second
    (a thread once start_thread() is called; the tests call it with their own clock).

    The dongle's hand-off (sizeup's claim on it is never let go in between, so no other app can take it there):
        _take     the job it runs stops, sizeup's claim stays and is rewritten "<system> test"; rtl_power sweeps
                  every side of the plan (outside every lock: Stop kills it)
        _give     the system's trunk-recorder gets it under the side's job (the claim rewritten again); the next
                  side is one restart of that trunk-recorder
        _switch   back: the test's job stops, the paused one starts, the claim rewritten (released only for Off)
    A restore that can't be done (another app got the dongle in between) is owed: tried again every OWED_RETRY_S
    until it is done, said in the station's health, and kept across a restart (reception_owed.json)."""

    def __init__(self, app, state_dir: Path | None, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = time.time):
        self.app, self.log, self.clock = app, log, clock
        self.dir = Path(state_dir) if state_dir else None
        self.marker = self.dir / "reception_test.json" if self.dir else None
        self.history_file = self.dir / "reception_history.json" if self.dir else None
        self.owed_file = self.dir / "reception_owed.json" if self.dir else None
        self.samples_dir = self.dir / "reception_samples" if self.dir else None
        self.active: dict | None = None
        self.probe: Probe | None = None
        self.probes: dict[str, ProbeRuntime] = {}  # (#436) the county's sites, probe-only: setup_probes()
        self._probes_cleaned = 0.0
        self.samples: list[dict] = []
        self.history: list[dict] = self._load_history()
        self.owed: dict | None = self._load_owed()
        # when each system's last test ended: a call of it that settles after that is the test's, never traffic
        self._ended: dict[str, float] = {}
        for h in self.history:
            if isinstance(h.get("ended"), (int, float)) and h.get("system"):
                self._ended[h["system"]] = max(self._ended.get(h["system"], 0.0), float(h["ended"]))
        self.sweep_fn = sweep                      # (binary, index, listed, center, rate, started=) -> dict | None
        self.on_end: Callable[[dict], None] = lambda t: None     # a test ended, or its paused job came back late
        self.inline = False                        # the tests: everything in the caller's thread
        self._lock = threading.RLock()
        self._saved = 0.0
        self._down_since: float | None = None
        self._sweep_proc = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wipe_samples()                       # whatever a hard kill left behind

    # -- files --------------------------------------------------------------------------------------------------------

    def _load_history(self) -> list[dict]:
        try:
            rows = json.loads(self.history_file.read_text()) if self.history_file and self.history_file.is_file() else []
        except (OSError, ValueError):
            rows = []
        return [r for r in rows if isinstance(r, dict)][:HISTORY_MAX] if isinstance(rows, list) else []

    def _load_owed(self) -> dict | None:
        try:
            o = json.loads(self.owed_file.read_text()) if self.owed_file and self.owed_file.is_file() else None
        except (OSError, ValueError):
            o = None
        ok = isinstance(o, dict) and isinstance(o.get("sdr"), str) and isinstance(o.get("was"), str) and o["was"] != OFF
        return dict(o, tried=0.0) if ok else None

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

    @staticmethod
    def _unlink(path: Path | None) -> None:
        try:
            if path is not None:
                path.unlink(missing_ok=True)
        except OSError:
            pass

    def _save_active(self) -> None:
        t = self.active
        if t is None:
            return
        doc = {k: v for k, v in t.items() if k != "ending"}
        if self.probe is not None:
            doc["running"] = self.probe.result()
        self._write(self.marker, doc)
        self._saved = self.clock()

    def _to_history(self, t: dict) -> None:
        t = {k: v for k, v in t.items() if k not in ("running", "cancel", "sweeps", "ending", "by")}
        self.history.insert(0, t)
        del self.history[HISTORY_MAX:]
        self._write(self.history_file, self.history)

    def _ended_hook(self, t: dict) -> None:
        try:
            self.on_end(t)
        except Exception as e:  # noqa: BLE001 - a log row, never in the way
            self.log(f"reception test: end not recorded: {type(e).__name__}: {e}")

    # -- what can be tested ---------------------------------------------------------------------------------------------

    def setup_probes(self) -> None:
        """(#436) The county's P25 system as a test can target it: a probe-only system per site, made from the county
        trunk-recorder's config (read only; sizeup/radio/probes.py), each with a trunk-recorder of its own that runs
        only on a lent dongle. The county's trunk-recorder, its state file and its dongles are never touched."""
        app = self.app
        cfg = getattr(app, "cfg", None)
        if (cfg is None or not getattr(app, "recorder", None) or not getattr(cfg, "p25_recorder", "")
                or not getattr(cfg, "p25_recorder_dir", "")):
            return
        from sizeup.radio import probes
        from sizeup.radio import recorder as trunk_recorder
        for sysd in probes.county(cfg.p25_recorder_dir, getattr(cfg, "p25_incident_talkgroups", ()),
                                  list(getattr(app, "modes", ())) + list(getattr(app, "_sys_jobs", {})), self.log):
            rec = trunk_recorder.make_system(sysd.name, sysd.label, cfg.p25_recorder, sysd.dir, self.log)
            self.probes[sysd.name] = ProbeRuntime(sysd, rec, app.tg, lambda rt, call: self.on_call(rt, call), self.log)
        probes.stale(cfg.p25_recorder_dir, list(self.probes), self.log)   # a site gone: its probe (marked) with it

    def targets(self) -> list:
        """Every system a test can be run on: the county's sites (probe-only), then the other systems."""
        return list(self.probes.values()) + list((getattr(self.app, "systems", None) or {}).values())

    def _rt(self, name):
        return (getattr(self.app, "systems", None) or {}).get(name) or self.probes.get(name)

    def _alert(self, rt) -> list[int]:
        tg = getattr(self.app, "tg", None)
        try:
            return alert_tgs(rt.system, (lambda t: tg.tier(t, rt.system.name))
                             if tg is not None and not rt.system.probe_only else None)
        except Exception:  # noqa: BLE001 - a column, never in the way
            return alert_tgs(rt.system)

    # -- which dongle ---------------------------------------------------------------------------------------------------

    def protected(self, job: str) -> str:
        """Why this job's dongle is never lent ("" when it may be): the fire side's own."""
        from sizeup.app import P25_MODE
        if job == P25_MODE:
            return "county fire dispatch (the P25 trunk)"
        kind = getattr(self.app, "_kind", {}).get(job)
        if kind == "paging":
            return "fire paging"
        if kind == "traffic":
            return "a fireground channel"
        return ""

    def dongles(self) -> list[dict]:
        """Each dongle with whether it can be lent now: {serial, name, job, present, held_by, protected, refuse}."""
        app = self.app
        sdr = app.sdr() if getattr(app, "shared", False) else None
        owed = self.owed
        out = []
        for x in (sdr or {}).get("sdrs", []):
            job = x.get("mode") or OFF
            why = self.protected(job) if job != OFF else ""
            refuse = ""
            if not x.get("present"):
                refuse = "unplugged"
            elif x.get("held_by"):
                refuse = f"in use by {x['held_by'].get('app') or 'another app'}"
            elif x.get("ambiguous"):
                refuse = "serial shared"
            elif job in getattr(app, "_sys_jobs", {}):
                refuse = f"runs {job}"
            elif why:
                refuse = "protected: not lendable"
            elif owed and owed["sdr"] == x["serial"]:
                refuse = f"{owed['was']} not restored yet"
            out.append({"serial": x["serial"], "name": x.get("name") or x["serial"], "job": job,
                        "present": bool(x.get("present")), "held_by": x.get("held_by"), "protected": why,
                        "refuse": refuse})
        return out

    def lent(self) -> tuple[str, str, str | None] | None:
        """(the dongle, the job it had, who set that) while a test has it or still owes it that job: what
        App._save_modes writes for it, so a restart of any release starts the dongle on its own job."""
        t = self.active
        if t is not None and t.get("taken"):
            return t["sdr"], t["was"], t.get("was_by")
        o = self.owed
        if o is not None:
            return o["sdr"], o["was"], o.get("was_by")
        return None

    def holds(self, serial: str) -> bool:
        t = self.active
        return t is not None and t["sdr"] == serial

    def lent_row(self, serial: str) -> dict | None:
        """The dongle's row in Settings > Radios while it is lent: {"label", "job", "paused"} ("owed": its job isn't
        back yet)."""
        t, o = self.active, self.owed
        if t is not None and t["sdr"] == serial:
            return {"label": t["label"], "job": t["plan"][min(t["phase"], len(t["plan"]) - 1)], "paused": t["was"]}
        if o is not None and o["sdr"] == serial:
            return {"label": o.get("label") or "", "job": "", "paused": o["was"], "owed": True}
        return None

    def person_switch(self, mode: str, sdr: str) -> str | None:
        """App.set_mode, before anything is switched: a person (or the hub) picks `mode` for a dongle. None: nothing
        of a test's, go on. Text: the answer instead (nothing else is switched):
          - the lent dongle: the test is stopped and its paused job comes back, whatever was picked (never left Off);
          - another dongle, a job of the system under test: refused;
          - a dongle still owed its job, that job picked ("restore now"): tried at once; it stays owed if it can't be.
        (Any other pick for an owed dongle goes on; the debt is dropped only once that switch has worked: switched().)"""
        t = self.active
        if t is not None:
            if sdr == t["sdr"]:
                self.stop(f"SDR {t['sdr_name']} was switched in Settings > Radios")
                back = f"{t['was']} comes back on it" if t["was"] != OFF else "it goes back to Off"
                return f"reception test stopped: SDR {t['sdr_name']} was lent to it; {back} (not switched to {mode})"
            rt = self._rt(t["system"])
            if rt is not None and mode in rt.system.jobs:
                return f"a reception test is running on {t['label']}: stop it first"
        o = self.owed
        if o is not None and sdr == o["sdr"] and mode == o["was"]:
            with self._lock:
                if self.owed is o:
                    o.pop("quiet", None)
                    self._retry_owed(self.clock(), now_please=True)
                if self.owed is o:
                    return (f"SDR {o.get('sdr_name') or sdr}: {o['was']} not restored yet ({o.get('err') or 'not started'}); "
                            "still owed to it, retrying")
            return ""
        return None

    def switched(self, sdr: str, mode: str) -> None:
        """App.set_mode, after a switch that worked: a dongle still owed a job has another one now by a person's
        choice (Off too), so the debt is dropped. (No lock: the ADS-B path calls set_mode under the mode lock.)"""
        o = self.owed
        if o is None or sdr != o["sdr"] or mode == o["was"]:
            return
        self.owed = None
        self._unlink(self.owed_file)
        self.log(f"reception test {o.get('id')}: SDR {o.get('sdr_name')} given {mode} by a person: {o['was']} no longer "
                 "owed to it")
        self._owed_note(o, True, f"left on {mode} (picked by a person)")
        self.app._save_modes()

    # -- what a page asks ---------------------------------------------------------------------------------------------

    def state(self) -> dict | None:
        """GET /api/reception (admins): the systems a test can be run on, the dongles, the running test and its
        numbers so far, the sample list, the history, an alert. None when this app has no system to test."""
        app = self.app
        if not self.targets():
            return None
        t, p, now = self.active, self.probe, self.clock()      # (no lock: a slow switch never holds the page up)
        run = None
        if t is not None:
            run = {k: t.get(k) for k in ("id", "system", "label", "sdr", "sdr_name", "was", "plan", "phase", "minutes",
                                         "started", "phase_started", "phase_until", "keep_min", "note")}
            run["halves"] = list(t.get("halves") or [])
            run["step"] = "stopping" if t.get("cancel") or t.get("ending") else t.get("step") or "running"
            run["now"] = round(now, 1)
            run["running"] = p.result() if p is not None and t.get("step") == "running" else None
        alert = self.health(now)
        return {"systems": [{"name": rt.system.name, "label": rt.system.label,
                             "kind": "county" if rt.system.probe_only else "system",
                             "jobs": [{"job": s.job, "mhz": round(s.center / 1e6, 5),
                                       "cc": [round(f / 1e6, 5) for f in rt.system.covered([s.index])]}
                                      for s in rt.system.sources],
                             "watch": list(rt.system.reception.get("watch") or []),
                             "alert": [{"tg": tg, "name": (rt.system.talkgroups.get(tg) or {}).get("name") or f"TG {tg}"}
                                       for tg in self._alert(rt)],
                             "caveat": str(rt.system.reception.get("caveat") or "")}
                            for rt in self.targets()],
                "durations": list(DURATIONS), "keep_min": KEEP_MIN_DEFAULT,
                "dongles": self.dongles(), "active": run, "alert": alert["text"] if alert else None,
                "samples": [dict(s, age_s=round(now - s["kept"])) for s in list(self.samples)],
                "history": [self._brief(h) for h in list(self.history)]}

    @staticmethod
    def _brief(h: dict) -> dict:
        out = {k: h.get(k) for k in ("id", "system", "label", "sdr", "sdr_name", "was", "plan", "minutes", "started",
                                     "ended", "status", "why", "note", "verdict", "restored", "restored_ok")}
        out["halves"] = [summary(x) for x in h.get("halves") or [] if isinstance(x, dict)]
        return out

    def health(self, now: float | None = None) -> dict | None:
        """For the station's health report: a job a test paused that is still paused when it shouldn't be.
        {job, sdr, since, text, push (it paused a job: a spare dongle left lent is the panel's only)} or None."""
        now = self.clock() if now is None else now
        t, o = self.active, self.owed
        if o is not None and not o.get("quiet"):
            return {"job": o["was"], "sdr": o.get("sdr_name") or o["sdr"], "since": o.get("since") or now, "push": True,
                    "text": f"{o['was']} on SDR {o.get('sdr_name') or o['sdr']} was not restored after the reception "
                            f"test ({o.get('err') or 'not started'}); retrying"}
        if t is not None and t.get("phase_until"):
            left = len(t["plan"]) - 1 - t["phase"]
            end = float(t["phase_until"]) + left * (t["minutes"] * 60 if t["minutes"] else 0)
            if now > end + OVERDUE_S:
                what = f"{t['was']} is still paused" if t["was"] != OFF else "still lent"
                return {"job": t["was"], "sdr": t["sdr_name"], "since": end, "push": t["was"] != OFF,
                        "text": f"SDR {t['sdr_name']}: {what}, {round((now - end) / 60)} min after its reception test "
                                "should have ended"}
        return None

    def post(self, body: dict, tid: str = "") -> str | None:
        """POST /api/reception: {"action": "start" | "stop" | "note", ...}. None, or why not. tid: the id a started
        test gets (the app's or the hub's own: who asked is kept there under it, never here)."""
        if not isinstance(body, dict):
            return "a JSON object"
        action = body.get("action")
        if action == "start":
            err = self.start(body, tid)
            if err:
                self.log(f"reception test refused: {err}")
            return err
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

    def start(self, o: dict, tid: str = "") -> str | None:
        app = self.app
        with self._lock:
            if self.active is not None:
                return "a reception test is already running: stop it first"
            if not getattr(app, "shared", False):
                return "each channel has its own dongle here: nothing to lend"
            jobs = o.get("jobs") if isinstance(o.get("jobs"), list) else [o.get("job")]
            plan = [j for j in jobs if isinstance(j, str)]
            every = self.targets()
            rt = self._rt(str(o.get("system") or "")) if o.get("system") else (
                next((x for x in every if plan and set(plan) <= set(x.system.jobs)), None)
                or (every[0] if len(every) == 1 else None))
            if rt is None:
                return f"no such system; one of {', '.join(x.system.name for x in every) or 'none'}"
            label = rt.system.label
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
            was = row["job"]
            if row["protected"]:
                return (f"SDR {name} runs {was}, {row['protected']}: protected: not lendable to a reception test. "
                        "Lend a spare or an airband dongle")
            if row["refuse"]:
                return f"SDR {name}: {row['refuse']}"
            if getattr(app, "adsb_sdr", "") == sdr:
                return f"SDR {name} is reserved for the ADS-B receiver"
            if was != OFF and o.get("pause") != was:
                return (f"SDR {name} runs {was}: it is paused for the test and comes back after. Confirm with "
                        f"\"pause\": \"{was}\"")
            now = self.clock()
            t = {"id": tid if SAMPLE_ID.fullmatch(str(tid)) else uuid.uuid4().hex[:12], "system": rt.system.name, "label": label, "sdr": sdr, "sdr_name": name,
                 "was": was, "was_by": app.set_by.get(sdr), "plan": plan, "phase": 0, "minutes": minutes,
                 "keep_min": keep, "note": str(o.get("note") or "").strip()[:NOTE_MAX], "started": round(now, 1),
                 "phase_started": None, "phase_until": None, "halves": [], "status": "running", "step": "starting",
                 "step_at": now, "taken": False}
            self.active = t
            self._save_active()                    # the job to give back is on disk before anything is switched
            self.log(f"reception test {t['id']}: {label} {' then '.join(plan)} on SDR {name}, "
                     + (f"{minutes} min a side" if minutes else "until stopped")
                     + (f"; {was} paused" if was != OFF else ""))
        self._spawn(self._begin)
        return None

    def stop(self, why: str = "stopped by a person") -> str | None:
        """End the test now. It answers at once: the dongle is given back by the test's own thread within a second
        (the outcome is in the history, and in the activity of who started it)."""
        t = self.active
        if t is None:
            return "no reception test is running"
        t.setdefault("cancel", why)
        p = self._sweep_proc
        if p is not None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001 - already gone
                pass
        if self.inline:
            self._guarded(self._stop_now)
        return None

    def _stop_now(self) -> None:
        with self._lock:
            t = self.active
            if t is not None and t.get("cancel") and t.get("step") != "starting":
                self._finish("stopped", t["cancel"])

    # -- running it -----------------------------------------------------------------------------------------------------

    def _spawn(self, fn) -> None:
        if self.inline:
            return fn()
        threading.Thread(target=self._guarded, args=(fn,), name="reception-test", daemon=True).start()

    def _guarded(self, fn) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - a test never leaves a dongle off its job
            self.log(f"reception test: {type(e).__name__}: {e}")
            try:
                with self._lock:
                    if self.active is not None:
                        self._finish("failed", f"{type(e).__name__}: {e}")
            except Exception as e2:  # noqa: BLE001 - nor does its thread ever die; tick() ends it again ("ending")
                self.log(f"reception test: not ended yet: {type(e2).__name__}: {e2}")

    def _begin(self) -> None:
        """This side of the plan onto the dongle (see the class)."""
        app = self.app
        with self._lock:
            t = self.active
            if t is None or t.get("step") != "starting":
                return
            if t.get("cancel"):
                return self._finish("stopped", t["cancel"])
            rt = self._rt(t["system"])
            first = not t.get("taken")
            if first:
                err = self._take(t)
                if err:
                    return self._finish("failed", err)
                self._save_active()
        if first:
            t["sweeps"] = self._sweeps(t, rt)      # no lock: Stop is immediate (it kills rtl_power)
        with self._lock:
            if self.active is not t:
                return
            if t.get("cancel"):
                return self._finish("stopped", t["cancel"])
            job, now = t["plan"][t["phase"]], self.clock()
            until = now + (t["minutes"] * 60 if t["minutes"] else UNTIL_STOPPED_MAX_S)
            rt.recorder.until = until + UNTIL_GRACE_S            # the trunk-recorder service's own deadline for it
            self.probe = rt.probe = Probe(rt.system, job, t["sdr"], self.clock, (t.get("sweeps") or {}).get(job),
                                          alert=self._alert(rt))
            err = self._give(t, rt, job)
            if err:
                return self._finish("failed", err)
            now = self.clock()
            self.probe.started = now
            t.update(phase_started=round(now, 1), phase_until=round(until, 1), step="running", step_at=now)
            self._down_since = None
            self._save_active()

    def _take(self, t: dict) -> str | None:
        """The dongle off its job and into the test's hands: sizeup's claim on it is kept, renamed."""
        app, sdr = self.app, t["sdr"]
        with app._mode_lock:
            app.switching = True
            try:
                cur = app.assign.get(sdr, OFF)
                if cur != t["was"]:
                    return f"SDR {t['sdr_name']} runs {cur} now, not {t['was']}"
                if cur != OFF:
                    app._stop_mode(cur, f"lent to a {t['label']} reception test", sdr, release=False)
                if not claims.claim(sdr, f"{t['label']} test"):
                    c = claims.other(sdr) or {}
                    return f"SDR {t['sdr_name']} was just taken by {c.get('app') or 'another app'}"
                t["taken"] = True
                app.assign[sdr] = OFF
                app.set_by[sdr] = "person"
                app._save_modes()                  # (saved under the job it had: lent())
            finally:
                app.switching = False
        return None

    def _sweeps(self, t: dict, rt) -> dict:
        """rtl_power over every side of the plan, while the test holds the dongle and before trunk-recorder has it:
        {job: sweep}. Nothing when rtl_power isn't there; a side it fails on is left out."""
        app, out = self.app, {}
        binary = rtl_power_path(getattr(app.cfg, "rtl_fm", "") or "")
        if not binary:
            return out
        from sizeup.radio import sdr as sdr_lookup

        def started(p) -> None:
            self._sweep_proc = p
            if t.get("cancel"):
                p.kill()
        for job in t["plan"]:
            if t.get("cancel"):
                break
            src = next(s for s in rt.system.sources if s.job == job)
            try:
                idx, _why = sdr_lookup.lookup(t["sdr"], app.cfg.rtl_fm)
                got = self.sweep_fn(binary, idx, rt.system.covered([src.index]), src.center, src.rate,
                                    started=started) if idx is not None else None
                if got:
                    out[job] = got
            except Exception as e:  # noqa: BLE001 - the level is an extra: the test goes on without it
                self.log(f"reception test {t['id']}: sweep of {job} failed: {type(e).__name__}: {e}")
            finally:
                self._sweep_proc = None
        return out

    def _give(self, t: dict, rt, job: str) -> str | None:
        """The system's trunk-recorder on the dongle under this side's job. The next side of a test is one restart
        of that trunk-recorder (its sources changed in one go), never a stop and a start with the dongle free between."""
        app, sdr = self.app, t["sdr"]
        with app._mode_lock:
            app.switching = True
            try:
                cur = app.assign.get(sdr, OFF)
                if cur in rt.system.jobs and cur != job:
                    rt.recorder.sources.pop(rt.job_source(cur), None)
                    if not getattr(rt.recorder, "unit", False):
                        claims.claim(sdr, job)
                    rt.recorder.give(rt.job_source(job), app._dongle(sdr), job)
                elif rt.system.probe_only:          # (#436) never a dongle job of the app: its trunk-recorder, here
                    if not claims.claim(sdr, job):
                        c = claims.other(sdr) or {}
                        return f"SDR {t['sdr_name']} was just taken by {c.get('app') or 'another app'}"
                    rt.recorder.give(rt.job_source(job), app._dongle(sdr), job)
                elif not app._start_mode(job, sdr):
                    c = claims.other(sdr) or {}
                    claims.release(sdr)
                    return f"SDR {t['sdr_name']} was just taken by {c.get('app') or 'another app'}"
                app.assign[sdr] = job
                app.set_by[sdr] = "person"
                app._save_modes()
            finally:
                app.switching = False
        return None

    def _switch(self, sdr: str, new: str, why: str) -> str | None:
        """The dongle from whatever of ours runs on it to `new` (a job, or Off), sizeup's claim kept across. Why not,
        when `new` can't start (then nothing of ours is on it and it is Off)."""
        import sizeup.app as appmod
        app = self.app
        with app._mode_lock:
            app.switching = True
            try:
                cur = app.assign.get(sdr, OFF)
                probe = next((rt for rt in self.probes.values() if cur in rt.system.jobs), None)
                if probe is not None:              # (#436) a probe's trunk-recorder: off, the claim kept
                    probe.recorder.take(probe.job_source(cur), why)
                elif cur != OFF:
                    app._stop_mode(cur, why, sdr, release=False)
                if new == OFF:
                    claims.release(sdr)
                    app.assign[sdr] = OFF
                    return None
                # claimed for the new job at once, before the pause that lets the dongle settle: on the Linux
                # station the trunk-recorder service has just let its own claim go
                if cur != OFF and claims.claim(sdr, new):
                    time.sleep(appmod.SWITCH_PAUSE_S)
                if not app._start_mode(new, sdr):
                    c = claims.other(sdr) or {}
                    claims.release(sdr)
                    app.assign[sdr] = OFF
                    return (f"in use by {c.get('app') or 'another app'} ({c.get('mode') or '?'})" if c
                            else f"{new} could not start on it")
                app.assign[sdr] = new
                return None
            finally:
                app.switching = False

    def _end_side(self) -> None:
        t, p = self.active, self.probe
        if t is None or p is None:
            return
        p.ended = self.clock()
        t["halves"].append(p.result())
        rt = self._rt(t["system"])
        if rt is not None and rt.probe is p:
            rt.probe = None
        self.probe = None

    def _finish(self, status: str, why: str = "") -> None:
        """The test is over (done, stopped, failed, interrupted): the dongle back on its job, the numbers to the
        history, what its trunk-recorder left on disk deleted."""
        t = self.active
        if t is None:
            return
        t.setdefault("ending", [status, why])      # if anything below raises, tick() comes back here until it is done
        status, why = t["ending"]
        self._end_side()
        self._probe_off(t, "the reception test ended", keep=True)
        ok, text = self._restore(t)
        self._probe_off(t, "the reception test ended")
        now = self.clock()
        t.update(restored=text, restored_ok=ok, status=status, why=why, ended=round(now, 1), step="ended",
                 verdict=overall(unjudged(t["halves"], status)))
        rt = self._rt(t["system"])
        self._ended[t["system"]] = now
        self.active = None
        self.app._save_modes()
        if rt is not None:
            rt.probe = None
            rt.recorder.until = None
            self._wipe_capture(rt)
        self._to_history(t)
        self._unlink(self.marker)
        v = t["verdict"]
        self.log(f"reception test {t['id']}: {status}" + (f" ({why})" if why else "")
                 + (f": {v['grade']}: {v['hint']}" if v else "") + f"; SDR {t['sdr_name']}: {text}")
        self._ended_hook(self.history[0])

    def _probe_off(self, t: dict, why: str, keep: bool = False) -> None:
        """(#436) A probe's trunk-recorder never outlives its test: every source it still has is taken (keep: but the
        one on the test's dongle while the dongle still runs it: the restore switches that one over, claim kept)."""
        rt = self.probes.get(t.get("system"))
        if rt is None:
            return
        cur = self.app.assign.get(t["sdr"], OFF)
        for k, (_fn, job) in list(rt.recorder.sources.items()):
            if keep and job == cur:
                continue
            try:
                rt.recorder.take(k, why)
            except Exception as e:  # noqa: BLE001 - said; the next end tries again
                self.log(f"reception test: {rt.system.label} probe not stopped: {type(e).__name__}: {e}")

    def _set_by(self, sdr: str, target: str, was: str, was_by) -> None:
        """Who set the dongle's job, as before the test. Off instead of the job it had (that job runs elsewhere now):
        nobody's choice, so never "person"."""
        if was_by in ("person", "auto") and (target != OFF or was == OFF):
            self.app.set_by[sdr] = was_by
        else:
            self.app.set_by.pop(sdr, None)

    def _restore(self, t: dict) -> tuple[bool, str]:
        """The dongle back on the job it had: (done, what was done in words). Not done: owed, tried again by tick()."""
        app, sdr, was = self.app, t["sdr"], t["was"]
        rt = self._rt(t["system"])
        if rt is not None:
            rt.recorder.until = None               # (before its trunk-recorder is asked anything else)
        if not t.get("taken"):
            return True, (f"{was} never left it" if was != OFF else "never taken")
        with app._mode_lock:
            cur = app.assign.get(sdr, OFF)
            if cur != OFF and cur not in t["plan"]:
                return True, f"left on {cur} (changed during the test)"
            elsewhere = was != OFF and any(o != sdr and m == was for o, m in app.assign.items())
            target = OFF if elsewhere else was
            err = self._switch(sdr, target, "the reception test ended")
            self._set_by(sdr, target, was, t.get("was_by"))
            if err:
                self.owed = {"id": t["id"], "sdr": sdr, "sdr_name": t["sdr_name"], "label": t["label"], "was": was,
                             "was_by": t.get("was_by"), "since": round(self.clock(), 1), "err": err, "tries": 1,
                             "tried": self.clock()}
                self._write(self.owed_file, self.owed)
        if err:
            return False, f"{was} NOT restored yet ({err}): retrying"
        return True, (f"{was} now on another dongle: Off" if elsewhere else f"{was} restored" if was != OFF else "Off again")

    def _retry_owed(self, now: float, now_please: bool = False) -> None:
        """A paused job that couldn't be given back: tried again until it is back (or a person gave the dongle
        something else, or the job runs on another dongle). now_please: a person asked ("restore now")."""
        o = self.owed
        if o is None or (not now_please and now - float(o.get("tried") or 0) < OWED_RETRY_S):
            return
        o["tried"] = now
        app, sdr, was = self.app, o["sdr"], o["was"]
        cur = app.assign.get(sdr, OFF)
        if cur == was:
            return self._owed_done(o, f"{was} restored" + (f" (after {o['tries']} tries)" if o.get("tries", 0) > 1 else ""),
                                   said=not o.get("quiet"))
        if cur != OFF:
            return self._owed_done(o, f"left on {cur}")
        if any(x != sdr and m == was for x, m in app.assign.items()):
            app.set_by.pop(sdr, None)
            return self._owed_done(o, f"{was} now on another dongle: Off")
        held = claims.other(sdr)
        err = (f"in use by {held.get('app') or 'another app'} ({held.get('mode') or '?'})" if held
               else self._switch(sdr, was, "restored after a reception test"))
        if err:
            first = bool(o.pop("quiet", None))
            o.update(err=err, tries=int(o.get("tries") or 0) + 1)
            if first:                              # the restart didn't get it going: said now
                self._owed_note(o, False, f"{was} NOT restored yet ({err}): retrying")
            self._write(self.owed_file, {k: v for k, v in o.items() if k != "tried"})
            return
        self._set_by(sdr, was, was, o.get("was_by"))
        app._save_modes()
        self._owed_done(o, f"{was} restored (after {int(o.get('tries') or 0) + 1} tries)")

    def _owed_note(self, o: dict, ok: bool, text: str) -> dict | None:
        row = next((h for h in self.history if h.get("id") == o.get("id")), None)
        if row is not None:
            row.update(restored=text, restored_ok=ok)
            self._write(self.history_file, self.history)
        return row

    def _owed_done(self, o: dict, text: str, said: bool = True) -> None:
        self.owed = None
        self._unlink(self.owed_file)
        self.app._save_modes()
        if not said:
            return
        row = self._owed_note(o, True, text)
        self.log(f"reception test {o.get('id')}: SDR {o.get('sdr_name')}: {text}")
        if row is not None:
            self._ended_hook(row)

    def recover(self) -> None:
        """sizeup starts and finds a test that never ended (a crash, a power cut, a kill: a clean stop ends it itself,
        shutdown()). It is closed as interrupted; its trunk-recorder, if the station's service still runs it, is
        stopped and the claim on the dongle seen gone; the dongle's job is set back before the dongles are started,
        and owed until it is seen running (tick)."""
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
            name = t.get("sdr_name") or sdr
            rt = self._rt(str(t.get("system")))
            if rt is not None:
                rt.probe, rt.recorder.until = None, None
                for _ in range(RECOVER_TRIES):     # off, and the dongle let go, before anything else opens it
                    if getattr(rt.recorder, "unit", False):
                        rt.recorder.stop("the reception test was ended by a restart")
                    c = claims.holder(sdr)
                    if c is None or claims._mine(c) or not claims._helper(c):
                        break
                self._ended[rt.system.name] = self.clock()
                self._wipe_capture(rt, force=True)
            cur = app.assign.get(sdr, OFF)
            if cur in t["plan"] or cur == was or cur == OFF:
                elsewhere = was != OFF and any(o != sdr and m == was for o, m in app.assign.items())
                target = OFF if elsewhere else was
                app.assign[sdr] = target
                self._set_by(sdr, target, was, t.get("was_by"))
                t["restored"], t["restored_ok"] = f"{target} set back at the restart", True
                if target != OFF:                  # until it is seen running there (the boot loop may be refused it)
                    self.owed = {"id": t.get("id"), "sdr": sdr, "sdr_name": name, "label": t.get("label") or "",
                                 "was": was, "was_by": t.get("was_by"), "since": round(self.clock(), 1), "err": "",
                                 "tries": 0, "tried": self.clock(), "quiet": True}
                    self._write(self.owed_file, {k: v for k, v in self.owed.items() if k not in ("tried", "quiet")})
                app._save_modes()
            else:
                t["restored"], t["restored_ok"] = f"left on {cur} (changed during the test)", True
            if isinstance(t.get("running"), dict):
                t.setdefault("halves", []).append(t["running"])
            t.update(status="interrupted", why="sizeup stopped without ending the test (a crash, a kill, the power)",
                     step="ended", ended=round(self.clock(), 1),
                     verdict=overall(unjudged([h for h in t.get("halves") or [] if isinstance(h, dict)], "interrupted")))
            self._to_history(t)
            self.log(f"reception test {t.get('id')}: interrupted by a restart; SDR {name}: {t['restored']}")
            self._ended_hook(self.history[0])
        self._unlink(self.marker)

    def tick(self) -> None:
        """Once a second: an owed job, a stop asked for, a side that is over, a test whose trunk-recorder never ran,
        the samples that are too old."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            self._clean_probes(now)
            self._retry_owed(now)
            t = self.active
            if t is None:
                return
            if t.get("ending"):                    # an end that didn't get through (it raised): again, until it does
                return self._finish(*t["ending"])
            if t.get("step") == "starting":
                if now - float(t.get("step_at") or now) > START_STUCK_S:
                    self._finish("failed", "the start never finished")
                return
            if t.get("cancel"):                    # Stop always works, whatever state the test is in
                return self._finish("stopped", t["cancel"])
            if self.probe is None:
                return self._finish("failed", "the test lost its measurement")
            app = self.app
            rt = self._rt(t["system"])
            if app.assign.get(t["sdr"]) != t["plan"][t["phase"]]:
                return self._finish("failed", f"SDR {t['sdr_name']} lost the test's job")
            state = rt.recorder.status().get("state")
            self.probe.ran(now, state == "ok")
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
                    t.update(step="starting", step_at=now)
                    self._save_active()
                    return self._spawn(self._begin)
                return self._finish("done")
            if now - self._saved >= SAVE_S:
                self._save_active()

    def start_thread(self) -> None:
        for rt in self.probes.values():            # (#436) the probes' calls: only ever a test's
            try:
                rt.ingest.start()
            except Exception as e:  # noqa: BLE001
                self.log(f"reception: {rt.system.label} probe's calls not watched: {type(e).__name__}: {e}")
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="reception-tests", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self._guarded(self.tick)
            except Exception as e:  # noqa: BLE001 - this thread gives dongles back: it never dies
                try:
                    self.log(f"reception tests: {type(e).__name__}: {e}")
                except Exception:  # noqa: BLE001
                    pass

    def shutdown(self) -> None:
        """sizeup is stopping (an update, a restart, a rollback): a running test ENDS here, its dongle back on its
        job and its trunk-recorder stopped before sizeup exits, so whatever release starts next finds things as they
        were before the test. (A kill or a power cut: recover() at the next start.)"""
        self._stop.set()
        t = self.active
        if t is not None:
            t.setdefault("cancel", "sizeup stopped")
            p = self._sweep_proc
            if p is not None:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            with self._lock:
                if self.active is not None:
                    self._finish("interrupted", "sizeup stopped during the test (an update or a restart)")
        except Exception as e:  # noqa: BLE001 - the marker stays: recover() at the next start
            self.log(f"reception test: not ended at the stop: {type(e).__name__}: {e}")
        for rt in self.probes.values():
            try:
                rt.ingest.stop()
            except Exception:  # noqa: BLE001
                pass
        self._wipe_samples()

    # -- the calls it captured ------------------------------------------------------------------------------------------

    def on_call(self, rt, call) -> bool:
        """A finished call of a system: True when it is a test's (counted, a sample kept; or one that settled after
        the test ended: dropped), so nothing else may take it."""
        app, name = self.app, rt.system.name
        with self._lock:
            p = rt.probe
            if p is None:
                ended = self._ended.get(name)
                permanent = any(m in rt.system.jobs for m in app.assign.values())
                return bool(ended and not permanent and float(call.start) < ended + LATE_CALL_S)
            row = p.call(call)
            t = self.active
            audio = getattr(call, "audio", None)
            # a sample only of what would be kept anyway: never a talkgroup switched Off, never an encrypted call
            from sizeup import tiers
            tg = int(call.talkgroup)
            if rt.system.probe_only:               # (#436) the county's: as its Record switch says, a known talkgroup
                info = rt.system.talkgroups.get(tg) or {}
                kept = bool(info) and not info.get("encrypted") and app.tg.record(tg)
            else:
                kept = app.tg.tier(tg, name) in tiers.RECORDED
            kept = kept and not getattr(call, "encrypted", False)
            if (kept and t is not None and t.get("keep_min") and audio is not None and len(audio)
                    and self.samples_dir is not None):
                sid = uuid.uuid4().hex[:12]
                try:
                    self.samples_dir.mkdir(parents=True, exist_ok=True)
                    (self.samples_dir / f"{sid}.wav").write_bytes(_wav_8k(audio))
                    self.samples.insert(0, dict(row, id=sid, kept=self.clock(), keep_s=float(t["keep_min"]) * 60, job=p.job))
                except OSError as e:
                    self.log(f"reception test: sample not kept: {type(e).__name__}: {e}")
                self._prune(self.clock())
            return True

    def _wipe_capture(self, rt, force: bool = False) -> None:
        """What the system's trunk-recorder wrote during the test and nobody took (a call still open at the end):
        deleted, unless the system has a dongle of its own now."""
        if not force and any(m in rt.system.jobs for m in self.app.assign.values()):
            return
        cap = Path(rt.system.capture_dir)
        for d in (cap, cap.with_name(cap.name + "-tmp")):     # and its temp directory (trunk-recorder's tempDir)
            try:
                kids = list(d.iterdir()) if d.is_dir() else []
            except OSError:
                kids = []
            for k in kids:
                try:
                    if k.is_dir() and not k.is_symlink():
                        shutil.rmtree(k, ignore_errors=True)
                    else:
                        k.unlink()
                except OSError:
                    pass

    def _clean_probes(self, now: float) -> None:
        """(#436) A probe's captures and temp files, capped while a test runs (an "until stopped" county test never
        fills the disk): janitor.clean_system every PROBE_CLEAN_S, calls older than PROBE_KEEP_S (a test's are taken
        within seconds) and at most PROBE_MAX_BYTES. The app's janitor cleans only the systems of the config."""
        if not self.probes or now - self._probes_cleaned < PROBE_CLEAN_S:
            return
        self._probes_cleaned = now
        from sizeup import janitor
        for rt in self.probes.values():
            try:
                got = janitor.clean_system(Path(rt.system.capture_dir), PROBE_KEEP_S, PROBE_MAX_BYTES, now=now)
                if got.get("over"):
                    self.log(f"reception: {rt.system.label} probe's captures over {PROBE_MAX_BYTES / 1e6:.0f} MB: "
                             f"{got['over']} oldest call(s) removed")
            except Exception as e:  # noqa: BLE001 - a cleanup, never in the way
                self.log(f"reception: {rt.system.label} probe's captures not cleaned: {type(e).__name__}: {e}")

    def _prune(self, now: float) -> None:
        keep = [s for s in self.samples if now - s["kept"] < s["keep_s"]][:SAMPLES_MAX]
        for s in self.samples:
            if s not in keep and self.samples_dir is not None:
                self._unlink(self.samples_dir / f"{s['id']}.wav")
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
