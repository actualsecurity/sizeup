"""Probe-only P25 systems (GitHub #436): the county's own P25 system as a reception test can target it (#410).

The county system is the main trunk-recorder's (<recorder_dir>/config.json, talkgroups.csv): Fire/EMS 15, on a fire
dongle that is never lent. A reception test of the county from another dongle (another antenna, another place) needs
a trunk-recorder of its own on that dongle. This module makes one, per site (each "p25" entry of the county's
"systems", with the county sources that cover its control channels):

    <recorder_dir>/systems/probe-<site>/
        config.json     the site's control channels and sources, its own shortName, captureDir and tempDir (beside the
                        county's, never in it), "controlWarnRate": -1 (the decode rate every 3 s), no unknown
                        talkgroups recorded; no "sizeup" block, so it is never a system of sizeup/radio/systems.py
        talkgroups.csv  the county's, copied
        probe.json      what marks it a probe (systems.discover leaves it out; the label shown)

It is written only when what it holds changed, and only there: the county's config.json, talkgroups.csv, state file
(p25_recorder.json) and capture directory are read or left alone, never written. On the station the trunk-recorder
service (sizeup/radio/trunkrecd.py SystemSupervisor) supervises it like any other system: off until a reception test
gives it a dongle, under its own state and status files, at a lower priority, and it stops at once and lets the dongle
go when the county's trunk-recorder is given that dongle. Its jobs are never a dongle job of Settings > Radios: only
the reception test (sizeup/radio/reception.py) runs one, on a lent dongle (never a fire one).

"TG hits/h" (the history's column): grants on the county's fire alert talkgroups ([p25] incident_talkgroups).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from sizeup.radio.systems import P25System, Source, read_talkgroups
from sizeup.radio.trunkrecd import SYSTEMS, covers

PREFIX = "probe-"
MARK = "probe.json"
NAME_MAX = 24                        # trunkrecd.SYSTEM_NAME
ANTENNA = "an outside antenna, higher, toward the county site"
# What of a county site is copied (an ALLOW-list, #436): how to decode and record it. Never an upload, a call log, an
# archive, an API key or a plugin's setting: a probe's calls are never sent or logged as the county's.
SITE_KEYS = ("type", "modulation", "control_channels", "squelch", "digitalLevels", "analogLevels", "compressWav",
             "minDuration", "minTransmissionDuration", "maxDuration", "hideEncrypted", "talkgroupDisplayFormat",
             "decodeMDC", "decodeFSync", "decodeStar", "decodeTPS", "conventionalTalkgroups")
# ... and of a county source: its span and how many recorders. Never its dongle ("device": the county's fire dongle),
# its frequency correction or its gain (that dongle's); the lent dongle gets PROBE_GAIN.
SOURCE_KEYS = ("center", "rate", "driver", "digitalRecorders", "analogRecorders")
PROBE_GAIN = 40


def is_probe(directory: str | Path) -> bool:
    return (Path(directory) / MARK).is_file()


def _name(short: str, used=()) -> str:
    """probe-<site>, at most NAME_MAX; two sites that come out the same (cut, or only other characters) get a short
    hash of their own name."""
    clean = re.sub(r"[^a-z0-9_-]", "", str(short).lower()) or "county"
    name = (PREFIX + clean)[:NAME_MAX]
    if name in used or clean != str(short).lower() or len(PREFIX + clean) > NAME_MAX:
        tag = hashlib.sha1(str(short).encode()).hexdigest()[:6]
        name = (PREFIX + clean)[:NAME_MAX - 7] + "-" + tag
    return name


def source_conf(x: dict) -> dict:
    out = {k: x[k] for k in SOURCE_KEYS if k in x}
    out["gain"] = PROBE_GAIN
    return out


def site_conf(s: dict, name: str, unit_script: str | None) -> dict:
    out = {k: s[k] for k in SITE_KEYS if k in s}
    out.update(shortName=name, talkgroupsFile="talkgroups.csv", recordUnknown=False, hideUnknownTalkgroups=True,
               transmissionArchive=False, callLog=False)
    if unit_script:
        out["unitScript"] = unit_script
    return out


def stale(recorder_dir: str | Path, keep, log=lambda m: None) -> list[str]:
    """Probe directories this release no longer makes (a site gone or renamed): removed. Only one with our marker
    (probe.json saying "probe": true) is ever removed; anything else under systems/ is never touched."""
    gone = []
    base = Path(recorder_dir) / SYSTEMS
    try:
        dirs = [d for d in base.iterdir() if d.name.startswith(PREFIX) and d.is_dir() and not d.is_symlink()]
    except OSError:
        return gone
    for d in dirs:
        if d.name in set(keep):
            continue
        try:
            mark = json.loads((d / MARK).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(mark, dict) and mark.get("probe") is True:
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d.name)
            log(f"reception: stale county probe {d.name} removed")
    return gone


def _write(path: Path, text: str) -> None:
    """Only when it changed (the service restarts a system whose files changed; nothing runs here, but still)."""
    try:
        if path.is_file() and path.read_text() == text:
            return
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def county(recorder_dir: str | Path, alert_tgs=(), taken_jobs=(), log=lambda m: None,
           write: bool = True) -> tuple[P25System, ...]:
    """A probe-only system per site of the county's trunk-recorder config (read only). taken_jobs: the dongle jobs
    already used (a probe's jobs never take one's name). write False: the definitions only, nothing written."""
    base = Path(recorder_dir)
    try:
        conf = json.loads((base / "config.json").read_text())
    except (OSError, ValueError) as e:
        log(f"reception: county probe not made: config.json: {type(e).__name__}")
        return ()
    if not isinstance(conf, dict):
        return ()
    srcs = [s for s in conf.get("sources") or [] if isinstance(s, dict)]
    sites = [s for s in conf.get("systems") or [] if isinstance(s, dict)
             and str(s.get("type") or "p25").lower() == "p25" and s.get("control_channels")]
    cap = Path(str(conf.get("captureDir") or "captures/p25"))
    cap = Path(os.path.abspath(cap if cap.is_absolute() else base / cap))
    taken = {str(j).lower() for j in taken_jobs}
    alert = sorted({int(t) for t in alert_tgs})
    out, used = [], set()
    for s in sites:
        short = str(s.get("shortName") or "county")
        name = _name(short, used)
        used.add(name)
        label = "County" if len(sites) == 1 else f"County {short}"
        try:
            ccs = [float(f) for f in s["control_channels"]]
            mine = [x for x in srcs if any(covers(x, f) for f in ccs)]
            centers = [float(x["center"]) for x in mine]
            rates = [float(x["rate"]) for x in mine]
        except (KeyError, TypeError, ValueError):
            log(f"reception: county probe {name} not made: its sources or control channels aren't numbers")
            continue
        if not mine:
            continue
        jobs = [f"{label} probe"] if len(mine) == 1 else [f"{label} {c / 1e6:.1f}" for c in centers]
        if any(j.lower() in taken for j in jobs) or len(set(jobs)) != len(jobs):
            log(f"reception: county probe {name} not made: a job name is taken ({', '.join(jobs)})")
            continue
        d = base / SYSTEMS / name
        pcap = cap.parent / f"p25-{name}"
        tgf = base / str(s.get("talkgroupsFile") or "talkgroups.csv")
        if not tgf.is_file():
            log(f"reception: county probe {name} not made: no {tgf.name}")
            continue
        try:
            tgs = read_talkgroups(tgf, name)
        except ValueError as e:
            log(f"reception: county probe {name} not made: {e}")
            continue
        if write:
            us = str(s.get("unitScript") or "")
            site = site_conf(s, name, (us if us.startswith("/") else os.path.relpath(base / us, d)) if us else None)
            tr = {"ver": conf.get("ver", 2), "sources": [source_conf(x) for x in mine], "systems": [site],
                  "captureDir": str(pcap), "tempDir": f"{pcap}-tmp", "controlWarnRate": -1,
                  "logLevel": conf.get("logLevel", "info")}
            try:
                d.mkdir(parents=True, exist_ok=True)
                _write(d / "config.json", json.dumps(tr, indent=2))
                _write(d / "talkgroups.csv", tgf.read_text())
                _write(d / MARK, json.dumps({"probe": True, "of": "county", "site": short, "label": label}))
                for x in (pcap, Path(f"{pcap}-tmp")):
                    x.mkdir(mode=0o750, parents=True, exist_ok=True)
            except OSError as e:
                log(f"reception: county probe {name} not written: {type(e).__name__}: {e}")
                continue
        out.append(P25System(
            name, label, f"County P25 ({short}), probe only", os.path.abspath(d), str(pcap),
            tuple(Source(i, jobs[i], centers[i], rates[i]) for i in range(len(mine))), tuple(ccs), tgs, {},
            {"watch": alert, "alert": alert, "antenna": ANTENNA, "caveat": ""}, probe_only=True))
    return tuple(out)
