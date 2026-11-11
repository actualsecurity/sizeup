"""More P25 systems than the county's (GitHub #409): MTA MRRS for Port Chester / Rye / Rye Brook PD and Metro-North.

Each directory <recorder_dir>/systems/<name>/ (the repo's deploy/trunk-recorder/systems/<name>/) is one system:

    config.json      trunk-recorder's own config for it (sources, control channels, captureDir), plus a "sizeup"
                     block trunk-recorder never sees: {"label": the system as shown ("MRRS"), "name": its full name,
                     "jobs": one dongle job per source, in order (["MRRS 770", "MRRS 773"]), "site": facts as listed}
    talkgroups.csv   trunk-recorder's talkgroups, plus a "Tier" column (sizeup/tiers.py); "Tag" is the UI group

Adding a talkgroup is one line in that file; adding a system is one directory. Nothing here is logic about a system.

How it runs (sizeup/app.py): every source is a job a dongle can be given in Settings > Radios, never by auto-assign.
The system's own trunk-recorder (sizeup/radio/recorder.py SystemRecorder; on the station sizeup-trunk-recorder's
SystemSupervisor, sizeup/radio/trunkrecd.py) runs the sources that have a dongle and nothing while none has, in its
own directory with its own log and capture directory, so the county's trunk-recorder is never touched. Its calls come
in through their own CallIngest: only the talkgroups in its file, each by its tier.

Rules that hold for every such system:
    - its traffic is context: never Main, never on our incident, never an incident source;
    - its radio IDs are never looked up in our radio registry (another system's numbers);
    - a talkgroup ID is unique across the systems here (checked when the config loads), so rows, clips and the
      "tg:<id>" keys stay as they are; each row also carries its `system`;
    - an encrypted talkgroup is never recorded: its grants are counted and kept as activity (time, talkgroup, radio ID);
    - a talkgroup granted on the system that isn't in the file is counted as unknown (TalkgroupPrefs.unknown).
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from sizeup import sourcekeys as sources, tiers
from sizeup.radio.trunkrecd import SYSTEMS, covers, system_dirs

GRANT = re.compile(r"SIZEUP-UNIT (?P<sys>\S+) (?P<src>-?\d+) call (?P<tg>\d+)")
ACTIVITY_MAX = 500           # encrypted key-ups remembered per system
ACTIVITY_SHOWN = 50
JOB = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ./_-]{0,39}")


class SystemError_(ValueError):
    """A system's directory can't be used (the reason; sizeup/config.py turns it into a ConfigError)."""


@dataclass(frozen=True)
class Source:
    index: int
    job: str                 # the dongle job that runs it ("MRRS 770")
    center: float            # Hz
    rate: float


@dataclass(frozen=True)
class P25System:
    name: str                # trunk-recorder's shortName and the directory ("mrrs")
    label: str               # as shown ("MRRS")
    title: str               # its full name
    dir: str
    capture_dir: str
    sources: tuple[Source, ...]
    frequencies: tuple[float, ...]                    # the listed (control channel candidate) frequencies, Hz
    talkgroups: dict[int, dict] = field(default_factory=dict)   # tg -> {name, group, encrypted, tier, system, description}
    site: dict = field(default_factory=dict)

    @property
    def jobs(self) -> tuple[str, ...]:
        return tuple(s.job for s in self.sources)

    def labels(self) -> dict[int, str]:
        return {tg: i["name"] for tg, i in self.talkgroups.items()}

    def covered(self, source_indexes) -> list[float]:
        """The listed frequencies the given sources take in."""
        srcs = [{"center": s.center, "rate": s.rate} for s in self.sources if s.index in set(source_indexes)]
        return [f for f in self.frequencies if any(covers(s, f) for s in srcs)]


def read_talkgroups(path: Path, system: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for r in csv.DictReader(path.read_text().splitlines()):
        try:
            tg = int(r["Decimal"])
        except (KeyError, TypeError, ValueError):
            raise SystemError_(f"{path}: a row without a Decimal talkgroup") from None
        enc = "E" in (r.get("Mode") or "").upper()
        listed = (r.get("Tier") or "").strip()
        if listed and tiers.clean(listed) is None:
            raise SystemError_(f"{path}: talkgroup {tg}: Tier {listed!r} is not one of {', '.join(tiers.TIERS)}")
        out[tg] = {"name": r.get("Alpha Tag") or f"TG {tg}", "group": r.get("Tag") or "", "encrypted": enc,
                   "tier": tiers.default(enc, listed), "system": system, "description": r.get("Description") or "",
                   "service": sources.service(r.get("Tag") or "", r.get("Service") or ""),
                   "department": (r.get("Department") or "").strip()}
    return out


def load(directory: Path) -> P25System:
    name = directory.name
    try:
        conf = json.loads((directory / "config.json").read_text())
    except (OSError, ValueError) as e:
        raise SystemError_(f"{directory / 'config.json'}: {type(e).__name__}: {e}") from None
    mine = conf.get("sizeup") if isinstance(conf.get("sizeup"), dict) else {}
    srcs = conf.get("sources") if isinstance(conf.get("sources"), list) else []
    systems = conf.get("systems") if isinstance(conf.get("systems"), list) else []
    if len(systems) != 1 or systems[0].get("shortName") != name:
        raise SystemError_(f"{directory}: config.json has one system, with \"shortName\": \"{name}\" (the directory's name)")
    jobs = mine.get("jobs") if isinstance(mine.get("jobs"), list) else []
    label = str(mine.get("label") or name)
    if len(jobs) != len(srcs) or not srcs:
        raise SystemError_(f"{directory}: \"sizeup\".\"jobs\" names one dongle job per source ({len(srcs)} source(s))")
    if len(set(jobs)) != len(jobs) or not all(isinstance(j, str) and JOB.fullmatch(j) for j in jobs):
        raise SystemError_(f"{directory}: \"sizeup\".\"jobs\": distinct short names, e.g. [\"{label} 1\", \"{label} 2\"]")
    try:
        sources = tuple(Source(i, jobs[i], float(s["center"]), float(s["rate"])) for i, s in enumerate(srcs))
        freqs = tuple(float(f) for f in systems[0].get("control_channels") or [])
    except (KeyError, TypeError, ValueError):
        raise SystemError_(f"{directory}: each source has a center and a rate, and control_channels are numbers") from None
    lost = [f for f in freqs if not any(covers({"center": s.center, "rate": s.rate}, f) for s in sources)]
    if lost:
        raise SystemError_(f"{directory}: no source covers {', '.join(f'{f / 1e6:.5f}' for f in lost)} MHz")
    tgf = directory / str(systems[0].get("talkgroupsFile") or "talkgroups.csv")
    if not tgf.is_file():
        raise SystemError_(f"{tgf} not found")
    cap = Path(str(conf.get("captureDir") or f"captures/p25-{name}"))
    cap = cap if cap.is_absolute() else (directory / cap)
    return P25System(name, label, str(mine.get("name") or label), os.path.abspath(directory), os.path.abspath(cap),
                     sources, freqs, read_talkgroups(tgf, name), dict(mine.get("site") or {}))


def discover(base: str | Path | None, taken_tgs=(), taken_jobs=()) -> tuple[P25System, ...]:
    """Every system under <base>/systems. A talkgroup ID or a job name already used (the county's talkgroups, the
    channel labels, an earlier system's) is refused: SystemError_."""
    if not base:
        return ()
    out = []
    tgs, jobs = set(taken_tgs), {str(j).lower() for j in taken_jobs}
    for name, d in system_dirs(base).items():
        s = load(d)
        both = sorted(set(s.talkgroups) & tgs)
        if both:
            raise SystemError_(f"{d}: talkgroup {both[0]} is already another system's (IDs are unique across the "
                               f"systems here: {SYSTEMS}/<name>/talkgroups.csv)")
        dup = [j for j in s.jobs if j.lower() in jobs]
        if dup:
            raise SystemError_(f"{d}: the job name {dup[0]!r} is already a channel's or another system's")
        tgs |= set(s.talkgroups)
        jobs |= {j.lower() for j in s.jobs}
        out.append(s)
    return tuple(out)


class Activity:
    """What a system's control channel says beyond its recorded calls, from the unit script's grant lines
    ("SIZEUP-UNIT mrrs <radio> call <tg>", one per key-up on every talkgroup): a talkgroup that isn't in the file is
    unknown (count, last heard: prefs.unknown_seen), and a key-up on an `activity` (encrypted) talkgroup is kept as
    (time, talkgroup, radio ID): nothing of it is ever recorded or decoded."""

    def __init__(self, system: P25System, prefs, log=lambda m: None, clock=time.time, path: Path | None = None,
                 on_minute=None):
        self.system, self.prefs, self.log, self.clock, self.path = system, prefs, log, clock, path
        # (source, minute, calls, radio IDs) once that minute is over: the radio log's activity table (#415)
        self.on_minute = on_minute
        self._minutes: dict[tuple[int, int], dict] = {}
        self._lock = threading.Lock()
        self.encrypted: deque[dict] = deque(maxlen=ACTIVITY_MAX)
        self._saved = 0.0
        try:
            if path and path.is_file():
                self.encrypted.extend(x for x in json.loads(path.read_text()) if isinstance(x, dict))
        except (OSError, ValueError):
            pass

    def feed_line(self, line: str) -> None:
        if "SIZEUP-UNIT " not in line:
            return
        m = GRANT.search(line)
        if not m or m["sys"] != self.system.name:
            return
        tg, src, now = int(m["tg"]), m["src"], self.clock()
        info = self.system.talkgroups.get(tg)
        if info is None:
            if self.prefs.unknown_seen(self.system.name, tg, now):
                self.log(f"p25 {self.system.name}: unknown talkgroup seen: {tg} (not in talkgroups.csv; counted in "
                         "Settings > Radios)")
            return
        if self.prefs.tier(tg, self.system.name) == tiers.ACTIVITY:
            self.prefs.seen(tg, system=self.system.name)
            rid = "" if src in ("-1", "0") else src
            with self._lock:
                self.encrypted.append({"at": round(now, 1), "tg": tg, "radio_id": rid})
                m = self._minutes.setdefault((tg, int(now // 60)), {"calls": 0, "radios": []})
                m["calls"] += 1
                if rid and rid not in m["radios"]:
                    m["radios"].append(rid)
                due = now - self._saved > 60
            if due:
                self.save()
        self.flush()

    def flush(self, everything: bool = False) -> None:
        """The minutes that are over (everything: the running one too, at shutdown) handed to on_minute."""
        cur = int(self.clock() // 60)
        with self._lock:
            done = {k: v for k, v in self._minutes.items() if everything or k[1] < cur}
            for k in done:
                del self._minutes[k]
        for (tg, minute), m in sorted(done.items(), key=lambda kv: kv[0][1]):
            if self.on_minute is not None:
                try:
                    self.on_minute(sources.p25(self.system.name, tg), minute, m["calls"], m["radios"])
                except Exception as e:  # noqa: BLE001 - a count, never in the way
                    self.log(f"p25 {self.system.name}: activity not logged: {type(e).__name__}: {e}")

    def save(self) -> None:
        if not self.path:
            return
        try:
            with self._lock:
                data, self._saved = json.dumps(list(self.encrypted)), self.clock()
            self.path.write_text(data)
        except OSError:
            pass

    def state(self) -> dict:
        self.flush()
        with self._lock:
            enc = [dict(e) for e in list(self.encrypted)[-ACTIVITY_SHOWN:][::-1]]
        for e in enc:
            e["name"] = self.prefs.name(e["tg"], self.system.name)
            e["source"] = sources.p25(self.system.name, e["tg"])
        return {"unknown": self.prefs.unknown(self.system.name), "encrypted": enc}


class Runtime:
    """One system as the app runs it: its trunk-recorder (recorder.SystemRecorder), its calls (p25.CallIngest: only
    the talkgroups in its file), the grants of its control channel (who keyed, for the finished calls' key-ups; the
    unknown and encrypted talkgroups: Activity)."""

    def __init__(self, system: P25System, recorder, prefs, on_call, log=lambda m: None, state_dir: Path | None = None,
                 on_activity=None):
        from sizeup.radio.live import LiveTalk
        from sizeup.radio.p25 import CallIngest
        self.system, self.recorder, self.prefs = system, recorder, prefs
        self.live = LiveTalk(tg_name=lambda tg: prefs.name(tg, system.name))          # grants and aliases only: never the "talking" highlight
        self.activity = Activity(system, prefs, log,
                                 path=(state_dir / f"p25_{system.name}_activity.json") if state_dir else None,
                                 on_minute=on_activity)
        self.ingest = CallIngest(system.capture_dir, system.labels(), lambda call: on_call(self, call), on_event=log,
                                 name=system.label)
        self.ingest.record_all = False                    # a talkgroup that isn't in its file is never taken
        self.ingest.grants = self.live.grants
        self.ingest.alias_of = lambda src: self.live.aliases.get(src, "")
        recorder.on_line = self._line

    def _line(self, line: str) -> None:
        self.live.feed_line(line)
        self.activity.feed_line(line)

    def job_source(self, job: str) -> int | None:
        return next((s.index for s in self.system.sources if s.job == job), None)

    def running(self, assigned) -> list[int]:
        """The sources whose job a dongle has."""
        have = set(assigned)
        return [s.index for s in self.system.sources if s.job in have]

    def status(self, assigned) -> dict:
        """Its row in the radio list (the header, Settings > Radios, the station health)."""
        st = self.ingest.status()
        rs = self.recorder.status()
        on = self.running(assigned)
        if not on:
            st.update(state="off", detail="no dongle (Settings > Radios)")
        elif rs["state"] != "ok":
            st.update(state=rs["state"], detail=rs["detail"])
        else:
            part = "" if len(on) == len(self.system.sources) else f"{len(on)} of {len(self.system.sources)} dongles · "
            st["state"] = "ok"
            st["detail"] = part + rs["detail"] + (" · " + st["detail"] if st.get("detail") else "")
        return dict(st, system=self.system.name, restarts=rs.get("restarts", 0))

    def state(self, assign: dict) -> dict:
        """state()["p25_systems"][k]: the system, its jobs and dongles, what runs, the control channel, and what its
        control channel showed (unknown talkgroups, encrypted activity)."""
        s = self.system
        by_job = {m: sd for sd, m in assign.items()}
        on = self.running(assign.values())
        row = self.status(assign.values())
        cc = getattr(self.recorder, "cc", None)
        return {"name": s.name, "label": s.label, "title": s.title, "site": s.site,
                "jobs": [{"job": x.job, "source": x.index, "mhz": round(x.center / 1e6, 5),
                          "span_mhz": round(x.rate / 1e6, 3), "sdr": by_job.get(x.job)} for x in s.sources],
                "frequencies": [round(f / 1e6, 5) for f in s.frequencies],
                "covered": [round(f / 1e6, 5) for f in s.covered(on)],
                "state": row["state"], "detail": row["detail"], "last_heard": row.get("last_heard"),
                "cc": cc.status() if cc is not None and on else None, **self.activity.state()}
