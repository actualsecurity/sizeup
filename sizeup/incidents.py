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

import threading
import time
import uuid
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
        return dict(self.__dict__)


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


class IncidentStore:
    def __init__(self, ours: set[str], geocode: Callable[[Dispatch], dict | None] = lambda d: None,
                 clock: Callable[[], float] = time.time):
        self.ours = ours                    # department names that are "us" (from the tone plan)
        self.geocode, self.clock = geocode, clock
        self._lock = threading.Lock()
        self.incidents: deque[Incident] = deque(maxlen=HISTORY)
        self.loose: deque[Traffic] = deque(maxlen=200)  # traffic with no active incident

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
                    return inc
        inc = Incident(uuid.uuid4().hex[:10], at, at, depts, d.incident_type, d.category, d.location,
                       d.cross_street, d.time_out, self.geocode(d) if d.location else None,
                       pages=[page_id], page_text=[d.text], page_starts=[at],
                       units=list(getattr(d, "units", [])), code=getattr(d, "code", ""))
        with self._lock:
            self.incidents.append(inc)
        return inc

    def on_traffic(self, t: Traffic) -> Incident | None:
        inc = self.active()
        with self._lock:
            if inc is None:
                self.loose.append(t)
                return None
            inc.traffic.append(t)
            inc.updated = max(inc.updated, t.end)
            return inc

    def close(self, incident_id: str) -> bool:
        with self._lock:
            for inc in self.incidents:
                if inc.id == incident_id:
                    inc.status = "closed"
                    return True
        return False

    def state(self) -> dict:
        self.active()  # age out quiet incidents
        with self._lock:
            return {"incidents": [i.as_dict() for i in reversed(self.incidents)],
                    "loose_traffic": [t.as_dict() for t in list(self.loose)[-50:]]}
