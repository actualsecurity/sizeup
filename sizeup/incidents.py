"""Incidents: a page opens one; radio traffic attaches to it while it is active.

    page (our department's tones, or our department named in the voice)
      -> Incident(type, location, cross street, time out, geocoded point)
    a repeat of the same page within REPEAT_S (dispatch reads it twice) -> merged, not a new one
    Fire/EMS 15 and Fireground 1 transmissions while an incident is active -> its traffic log
    no traffic for QUIET_CLOSE_S -> closed (kept in history)

Nothing is invented: an incident with no parsed type says "Unknown", one without a geocode
has no map point and the map stays on the home view.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import fields
from pathlib import Path
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from sizeup.dispatch import Dispatch

REPEAT_S = 300          # same location paged again within 5 min = the repeat
QUIET_CLOSE_S = 3600    # an hour with no page or traffic -> closed
HISTORY = 50


@dataclass
class Traffic:
    id: str
    source: str             # "South 2" | "Fire/EMS 15" | "FG1" ...
    start: float
    end: float
    text: str
    status: str = "done"
    unit: str = ""          # talker (P25 radio ID / alias) when known
    talkgroup: int | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "raw_audio"}

    @classmethod
    def from_dict(cls, d: dict) -> Traffic:
        names = {f.name for f in fields(cls)}
        t = cls(**{k: v for k, v in d.items() if k in names})
        for k, v in d.items():           # extras the pipeline added (raw, confidence, ...)
            if k not in names and k != "has_audio":
                setattr(t, k, v)
        return t


@dataclass
class Incident:
    id: str
    opened: float
    updated: float
    departments: list[str]
    incident_type: str
    category: str
    location: str
    cross_street: str
    time_out: str
    geo: dict | None = None             # {lat, lon, label, kind, confidence}
    units: list[str] = field(default_factory=list)   # dispatched apparatus, "Engine 61"
    code: str = ""                      # priority dispatch code, "69-D-6"
    status: str = "active"              # active | closed
    pages: list[str] = field(default_factory=list)
    page_text: list[str] = field(default_factory=list)
    page_starts: list[float] = field(default_factory=list)   # aligned with pages / page_text
    traffic: list[Traffic] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("traffic", "page_starts")}
        d["traffic"] = [t.as_dict() for t in self.traffic[-200:]]
        d["page_entries"] = [{"id": i, "text": t, "start": s, "label": "South 2"}
                             for i, t, s in zip(self.pages, self.page_text, self.page_starts)]
        return d

    def to_json(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "traffic"}
        d["traffic"] = [t.as_dict() for t in self.traffic]
        return d

    @classmethod
    def from_json(cls, d: dict) -> Incident:
        names = {f.name for f in fields(cls)}
        inc = cls(**{k: v for k, v in d.items() if k in names and k != "traffic"})
        inc.traffic = [Traffic.from_dict(t) for t in d.get("traffic", [])]
        return inc


class IncidentStore:
    def __init__(self, ours: set[str], geocode: Callable[[Dispatch], dict | None] = lambda d: None,
                 clock: Callable[[], float] = time.time, path: str | Path | None = None,
                 keep_s: float = 24 * 3600):
        self.ours = ours                    # department names that are "us" (from the tone plan)
        self.geocode, self.clock = geocode, clock
        self._lock = threading.Lock()
        self.incidents: deque[Incident] = deque(maxlen=HISTORY)
        self.loose: deque[Traffic] = deque(maxlen=1000)  # traffic with no active incident / other talkgroups
        # Saved on every change and reloaded on start, so a restart doesn't wipe the screen.
        self.path, self.keep_s = (Path(path) if path else None), keep_s
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        cutoff = self.clock() - self.keep_s
        for i in d.get("incidents", []):
            if i.get("updated", 0) >= cutoff:
                self.incidents.append(Incident.from_json(i))
        for t in d.get("loose", []):
            if t.get("end", 0) >= cutoff:
                self.loose.append(Traffic.from_dict(t))

    def prune(self, cutoff: float) -> None:
        """Forget closed incidents and loose traffic older than the history limit."""
        with self._lock:
            n0 = len(self.incidents) + len(self.loose)
            keep = [i for i in self.incidents if i.status == "active" or i.updated >= cutoff]
            self.incidents.clear()
            self.incidents.extend(keep)
            loose = [t for t in self.loose if t.end >= cutoff]
            self.loose.clear()
            self.loose.extend(loose)
            changed = n0 != len(self.incidents) + len(self.loose)
        if changed:
            self.save()

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            d = {"incidents": [i.to_json() for i in self.incidents], "loose": [t.as_dict() for t in self.loose]}
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, self.path)
        except OSError:
            pass  # disk trouble must never stop the display

    def active(self) -> Incident | None:
        now = self.clock()
        with self._lock:
            for inc in reversed(self.incidents):
                if inc.status == "active" and now - inc.updated > QUIET_CLOSE_S:
                    inc.status = "closed"
            return next((i for i in reversed(self.incidents) if i.status == "active"), None)

    def on_page(self, page_id: str, tone_departments: list[str], d: Dispatch, at: float | None = None) -> Incident | None:
        """A transcribed page. Opens/merges an incident when it is for one of our departments."""
        at = self.clock() if at is None else at
        depts = list(dict.fromkeys(tone_departments + d.departments))
        if not any(x in self.ours for x in depts):
            return None
        with self._lock:
            for inc in reversed(self.incidents):
                if inc.status == "active" and at - inc.opened < REPEAT_S and (
                        (d.location and d.location == inc.location) or not d.location
                        or (d.incident_type == inc.incident_type and d.incident_type != "Unknown")):
                    inc.pages.append(page_id)
                    inc.page_text.append(d.text)
                    inc.page_starts.append(at)
                    inc.updated = at
                    for k in ("location", "cross_street", "time_out"):
                        if not getattr(inc, k) and getattr(d, k):
                            setattr(inc, k, getattr(d, k))
                    if inc.incident_type == "Unknown" and d.incident_type != "Unknown":
                        inc.incident_type, inc.category = d.incident_type, d.category
                    inc.code = inc.code or getattr(d, "code", "")
                    for u in getattr(d, "units", []):
                        if u not in inc.units:
                            inc.units.append(u)
                    if inc.geo is None and d.location:
                        inc.geo = self.geocode(d)
                    for x in depts:
                        if x not in inc.departments:
                            inc.departments.append(x)
                    merged = inc
                    break
            else:
                merged = None
        if merged is not None:
            self.save()
            return merged
        inc = Incident(uuid.uuid4().hex[:10], at, at, depts, d.incident_type, d.category, d.location,
                       d.cross_street, d.time_out, self.geocode(d) if d.location else None,
                       pages=[page_id], page_text=[d.text], page_starts=[at],
                       units=list(getattr(d, "units", [])), code=getattr(d, "code", ""))
        with self._lock:
            self.incidents.append(inc)
        self.save()
        return inc

    def on_traffic(self, t: Traffic, attach: bool = True) -> Incident | None:
        """attach=False: context traffic (another battalion's talkgroup) goes to the loose list."""
        inc = self.active() if attach else None
        with self._lock:
            if inc is not None:
                inc.traffic.append(t)
                inc.updated = max(inc.updated, t.end)
            else:
                self.loose.append(t)
        self.save()
        return inc

    def retext(self, tid: str, text: str, status: str, confidence: float | None = None,
               model: str = "", at: float | None = None) -> bool:
        """A re-run of speech-to-text for one transmission or page: new text in place, the old text
        kept in `history` (newest last, up to 5) so it can be compared."""
        at = self.clock() if at is None else at
        hit = False
        with self._lock:
            for t in [t for i in self.incidents for t in i.traffic] + list(self.loose):
                if t.id != tid:
                    continue
                hit = True
                if t.text != text:
                    hist = list(getattr(t, "history", None) or [])
                    hist.append({"text": t.text, "at": at, "model": model})
                    t.history = hist[-5:]  # type: ignore[attr-defined]
                t.text, t.status = text, status
                t.confidence, t.rerun_at = confidence, at  # type: ignore[attr-defined]
            for inc in self.incidents:
                for k, pid in enumerate(inc.pages):
                    if pid == tid and k < len(inc.page_text):
                        inc.page_text[k], hit = text, True
        if hit:
            self.save()
        return hit

    def close(self, incident_id: str) -> bool:
        with self._lock:
            found = next((inc for inc in self.incidents if inc.id == incident_id), None)
            if found:
                found.status = "closed"
        if found:
            self.save()
        return found is not None

    def state(self) -> dict:
        self.active()  # age out quiet incidents
        with self._lock:
            return {"incidents": [i.as_dict() for i in reversed(self.incidents)],
                    "loose_traffic": [t.as_dict() for t in list(self.loose)[-300:]]}
