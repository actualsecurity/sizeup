"""Antenna groups: a set of dongles locked to a set of jobs (Settings > Radios > Receivers).

The station's dongles sit on different antenna feeds (a 700/800 MHz antenna for one P25 system, a shared UHF one for
paging, the fireground and the county's system, an airband one). A dongle on the wrong feed is useless for a job, so
an admin can lock some dongles to some jobs:

    {"name": "UHF", "sdrs": ["<serial>", ...], "jobs": ["Fire/EMS 15", "South 2", "FG1", "FG2"]}

    sdrs   the dongles on that feed (exact serials)
    jobs   the jobs that feed carries, highest priority first

Rules (sizeup/app.py applies them):
    - a grouped dongle only runs its group's jobs (or Off);
    - a group's jobs only run on its own dongles: never moved to a dongle outside the group, by a person or by
      auto-assign;
    - a dongle in no group, and a job in no group, behave as they always did;
    - within a group, the admin's own mapping (which dongle runs which job) is kept while every dongle is there. When
      fewer of them can run (one unplugged, failed, held by another app, its serial shared by two), the ones left run
      the highest-priority jobs of that mapping (plan()): the lowest-priority job gives its dongle up to a higher
      one whose dongle went. When the dongle is back, the mapping is put back as the admin left it.

What a fallback may never do (plan()):
    - turn on a dongle a person switched Off (that is how a dongle is handed to another app, shortfinal);
    - take a fire job's dongle (the county's trunk-recorder, paging, a fireground) for a job that isn't one, whatever
      the order says;
    - run a job that can't run on the dongles it would have: another P25 system's side whose source covers none of
      its control channels on its own (trunkrecd.covers), the county's trunk-recorder when its config needs more than
      one dongle.

One job that needs two dongles (MRRS 770 + MRRS 773) is two jobs here, one per source: with one dongle left, the
higher of the two runs and the system decodes what that side covers (Runtime.status: "1 of 2 dongles").

Kept on the station in antenna_groups.json next to sdr_modes.json; the hub's Settings sends a change there as
a job, like a dongle's job (set_antenna_groups), and shows what the station reports (App.sdr()["groups"]).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

OFF = "Off"
FILE = "antenna_groups.json"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ./+&_-]{0,23}")
SERIAL = re.compile(r"[A-Za-z0-9._-]{1,32}")
MAX_GROUPS = 8
MAX_SDRS = 16


@dataclass(frozen=True)
class Group:
    name: str
    sdrs: tuple[str, ...]
    jobs: tuple[str, ...]            # highest priority first

    def row(self) -> dict:
        return {"name": self.name, "sdrs": list(self.sdrs), "jobs": list(self.jobs)}


def parse(raw) -> tuple[list[Group], str]:
    """A list of {name, sdrs, jobs} as sent by Settings: (groups, "") or ([], why not). Only the shape is checked
    here; what the station has (its jobs and dongles) is App.set_antenna_groups'."""
    if not isinstance(raw, list):
        return [], "groups: a list of {name, sdrs, jobs}"
    if len(raw) > MAX_GROUPS:
        return [], f"at most {MAX_GROUPS} groups"
    out: list[Group] = []
    names: set[str] = set()
    sdr_in: dict[str, str] = {}
    job_in: dict[str, str] = {}
    for g in raw:
        if not isinstance(g, dict):
            return [], "each group: {name, sdrs, jobs}"
        name = str(g.get("name") or "").strip()
        if not NAME.fullmatch(name):
            return [], f"group name {name[:30]!r}: 1-24 letters, digits, space . / + & _ -"
        if name.lower() in names:
            return [], f"two groups named {name}"
        names.add(name.lower())
        sdrs, jobs = g.get("sdrs"), g.get("jobs")
        if not isinstance(sdrs, list) or not isinstance(jobs, list) \
                or not all(isinstance(x, str) for x in sdrs) or not all(isinstance(x, str) for x in jobs):
            return [], f"{name}: sdrs and jobs are lists of names"
        sdrs = [s.strip() for s in sdrs]
        jobs = [j.strip() for j in jobs]
        if not sdrs or len(sdrs) > MAX_SDRS or not all(SERIAL.fullmatch(s) for s in sdrs):
            return [], f"{name}: 1-{MAX_SDRS} dongles, by serial"
        if not jobs or OFF in jobs:
            return [], f"{name}: at least one job (not Off)"
        if len(set(sdrs)) != len(sdrs) or len(set(jobs)) != len(jobs):
            return [], f"{name}: a dongle or a job listed twice"
        for s in sdrs:
            if s in sdr_in:
                return [], f"SDR {s} is in {sdr_in[s]} and {name}: one group each"
            sdr_in[s] = name
        for j in jobs:
            if j in job_in:
                return [], f"{j} is in {job_in[j]} and {name}: one group each"
            job_in[j] = name
        out.append(Group(name, tuple(sdrs), tuple(jobs)))
    return out, ""


def load(path: Path | None) -> list[Group]:
    """The groups saved (none: [], as before groups existed). A file that doesn't read: none, said by the caller."""
    if path is None or not path.is_file():
        return []
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    groups, _why = parse(d.get("groups") if isinstance(d, dict) else None)
    return groups


def save(path: Path, groups: Iterable[Group]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps({"groups": [g.row() for g in groups], "at": round(time.time(), 1)}))
    os.replace(tmp, path)


def of_sdr(groups: Iterable[Group], serial: str) -> Group | None:
    return next((g for g in groups if serial in g.sdrs), None)


def of_job(groups: Iterable[Group], job: str) -> Group | None:
    return next((g for g in groups if job in g.jobs), None)


def plan(group: Group, own: dict[str, str], usable: Iterable[str], spares: Iterable[str] = (),
         current: dict[str, str] | None = None, fire: Callable[[str], bool] = lambda j: False,
         runnable: Callable[[str, set[str]], bool] = lambda j, on: True) -> tuple[dict[str, str], list[str]]:
    """What each usable dongle of `group` runs now: ({serial: job or Off}, the wanted jobs left without a dongle).

    own       each of the group's dongles' own job (the admin's mapping; Off for none)
    usable    the group's dongles that can run a job now (plugged in, not another app's, not lent to a test)
    spares    usable dongles whose own job is Off and that may stand in (not switched Off by a person)
    current   what each runs now: a stand-in already running a job keeps it (no shuffling at every look)
    fire      a fire job: its dongle never goes to a job that isn't one
    runnable  runnable(job, jobs that would run with it): a job that can run on what it would have

    The wanted jobs are the group's jobs that are some dongle's own. A usable dongle runs its own job, unless a
    higher-priority wanted job has no usable dongle: then the lowest-priority job runs no more and its dongle runs that
    one (a spare first, if there is one). Every dongle back: each runs its own job again."""
    prio = {j: k for k, j in enumerate(group.jobs)}
    usable = [s for s in group.sdrs if s in set(usable)]
    current = current or {}
    out = {s: (own.get(s, OFF) if own.get(s, OFF) in prio else OFF) for s in usable}
    free = [s for s in group.sdrs if s in set(spares) and s in out and out[s] == OFF]
    wanted = sorted({own.get(s, OFF) for s in group.sdrs} & set(prio), key=prio.__getitem__)
    running = {j for j in out.values() if j != OFF}
    stood_in: set[str] = set()                        # dongles already given another's job in this plan
    for job in [j for j in wanted if j not in running]:
        # a spare first (the one already running it, if any), else the dongle with the lowest-priority job below it
        cands = sorted(free, key=lambda s: current.get(s) != job)
        if not cands:
            lower = [s for s in usable if s not in stood_in and out[s] != OFF and prio[out[s]] > prio[job]
                     and not (fire(out[s]) and not fire(job))]
            cands = sorted(lower, key=lambda s: -prio[out[s]])
        for s in cands:
            after = (running - {out[s]}) | {job}
            if not runnable(job, after):
                continue
            running = after
            out[s] = job
            stood_in.add(s)
            if s in free:
                free.remove(s)
            break
    return out, [j for j in wanted if j not in running]
