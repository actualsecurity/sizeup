"""Data sources (GitHub #186): every data pull the hub keeps, in one place, with Refresh now and a schedule.

User, 2026-10-15: "i also want a way in settings to refresh data cache on some of our data pulls", then "and maybe
we can refresh some of them on a regular basis so we dont need to force it to?".

A light registry over the providers' own code (it never fetches anything itself): each pull registers a Source with
its key, name and group, a status() read from the provider (last fetched, next due, error, items, bytes), the
provider's own refresh path, and how it is scheduled. Settings > Data sources (static/datasources.js) lists them.

Two kinds of schedule:

  built-in   the provider keeps its own (weather every 2 / 10 min, the Thruway's events and signs, trains and aircraft
             while someone looks): shown as it is ("every 2 min"), next due from the provider. Refresh now runs the
             provider's fetch at once, which also restarts its own schedule.
  managed    data that changes slowly and was fetched on the provider's own long timer (or once): the county GIS
             layers, the camera lists, Metro-North's static GTFS, the FAA registry, the airport layer and the tone
             list files. The registry runs these (one scheduler thread, one at a time) and the provider's own timer
             for that pull is held (hold(): its "next" pinned to never), so nothing fetches twice. Each has a default
             interval (off / hourly / daily / weekly / monthly) an admin can change in Settings; the choice is kept
             in the CAD's settings (section "datasources", key -> choice; audited, exported, imported like the rest;
             sizeup/cad/settings.py _ds_op). Next due = last fetched + interval + jitter (a few % of the interval,
             at most JITTER_MAX_S, so the county's layers don't all go at once); what is due runs one at a time
             (never fetched: at once, in registration order). A heavy pull (quiet: the GIS layers, the static GTFS,
             the FAA registry) on a daily or longer interval waits for the quiet hours (QUIET_HOURS, local time) once
             it has data. A failure backs off (BACKOFF_S doubling, never longer than the interval) and shows its
             error; a refresh, manual or scheduled, restarts the schedule from when it ran.

Refresh now (POST /api/cad/datasources/<key>/refresh, admins only; sizeup/cad/api.py): never two of one source at
once, at most one per source per min_gap_s (MANUAL_GAP_S; an hour for the FAA registry), and not while the provider
itself is resting after the source refused or rate-limited us (blocked()). It runs on its own thread and the request
returns at once; the panel polls GET /api/cad/datasources while it is open (nothing goes in the state push). Each one
is in the person's activity and the Settings audit log. "Clears the cache" means the provider fetches afresh,
ignoring what it holds, and replaces it only with a good answer: a failed refresh never empties a layer.

Registering a new pull (road projects, #185, and whatever comes after): one register() call where the app builds
the providers (build() below), e.g.

    ds.register(Source("roadwork", "Road projects", "Map layers", source="NYSDOT", link="https://...",
                       status=lambda: {"at": p.at, "error": p.error, "n": len(p.items)},
                       refresh=p.refresh, every="daily", hold=lambda: p.status.update(next=HELD)))

status() keys: at (unix s, last good fetch), error ("" when fine), n (items), bytes, next (the provider's own next
fetch: built-in only), on (False: not configured, with why), note. refresh() -> bool (True: read), never raises
(an exception is caught and shown as the error).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
SECTION = "datasources"                      # the CAD settings' section: key -> choice
CHOICES = {"off": None, "hourly": 3600.0, "daily": 86400.0, "weekly": 7 * 86400.0, "monthly": 30 * 86400.0}
GROUPS = ("Map layers", "Cameras", "Transit", "Weather", "Aircraft", "Radio")
KEY_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")
HELD = float("inf")                          # a provider's own "next" for a pull the registry runs
MANUAL_GAP_S = 120.0                         # Refresh now: at most once per source this often
HEAVY_GAP_S = 3600.0                         # ... a heavy one (the FAA registry, ~70 MB)
BACKOFF_S = (300.0, 6 * 3600.0)              # a scheduled pull that failed: 5 min doubling to 6 h (never over its interval)
JITTER = 0.05                                # next due moves up to this fraction of the interval ...
JITTER_MAX_S = 3600.0                        # ... at most this
QUIET_HOURS = (1, 5)                         # heavy pulls between 01:00 and 05:00 local
TICK_S = 30.0


class TooSoon(Exception):
    """A manual refresh refused: running already, too soon after the last, or the source is resting."""


@dataclass
class Source:
    key: str
    name: str
    group: str
    source: str = ""                          # who provides the data ("Westchester County GIS")
    link: str = ""
    attribution: str = ""
    status: Callable[[], dict] = dict
    refresh: Callable[[], bool] | None = None    # None: shown, never refreshed here
    every: str = ""                           # the managed default (a CHOICES key); "" = built-in or none
    builtin: str = ""                         # the provider's own cadence in words, when it keeps one
    hold: Callable[[], None] | None = None    # pin the provider's own timer for this pull (managed)
    min_gap_s: float = MANUAL_GAP_S
    quiet: bool = False                       # scheduled runs in the quiet hours (heavy)
    blocked: Callable[[], str] | None = None  # why a refresh must wait now (the source rate-limited us), else ""
    note: str = ""
    action: str = "Refresh now"               # the button ("Reload" for a file read from disk)

    @property
    def managed(self) -> bool:
        return self.every in CHOICES and self.refresh is not None


def _safe(fn: Callable[[], dict] | None) -> dict:
    try:
        out = fn() if fn else {}
        return out if isinstance(out, dict) else {}
    except Exception as e:  # a provider's trouble shows on its row, never breaks the page
        return {"error": f"status: {type(e).__name__}: {e}"}


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != HELD else None


class DataSources:
    def __init__(self, choices: Callable[[], dict] = dict, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, rng: Callable[[], float] = random.random,
                 spawn: Callable[[Callable[[], None], str], None] | None = None, tz=TZ):
        """choices(): the admins' interval choices (key -> CHOICES key; the CAD settings' "datasources").
        spawn(fn, name): runs a manual refresh off the request thread (tests: at once)."""
        self.choices, self.log, self.clock, self.rng, self.tz = choices, log, clock, rng, tz
        self.spawn = spawn or (lambda fn, name: threading.Thread(target=fn, name=name, daemon=True).start())
        self.sources: dict[str, Source] = {}
        self.state: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = clock()

    # -- registration ----------------------------------------------------------------------------------------

    def register(self, src: Source) -> Source:
        if not KEY_RE.fullmatch(src.key):
            raise ValueError(f"data source key {src.key!r}: lower case letters, digits and _ . : - (at most 64)")
        if src.group not in GROUPS:
            raise ValueError(f"data source {src.key}: group one of {', '.join(GROUPS)}")
        if src.every and src.every not in CHOICES:
            raise ValueError(f"data source {src.key}: every one of {', '.join(CHOICES)}")
        with self._lock:
            if src.key in self.sources:
                raise ValueError(f"data source {src.key} registered twice")
            order = len(self.sources)             # a tie (never fetched, all due at start): registration order
            self.sources[src.key] = src
            self.state[src.key] = {"running": None, "started": None, "finished": None, "ok": None, "error": "",
                                   "fails": 0, "tried": None, "manual": None, "by": "", "why": "",
                                   "spread": self.rng(), "order": order}
        self._hold(src)
        return src

    def _hold(self, src: Source) -> None:
        if src.managed and src.hold is not None:
            try:
                src.hold()
            except Exception as e:  # pragma: no cover - a provider without that field: its own timer runs too
                self.log(f"datasources: {src.key}: its own schedule not held ({type(e).__name__}: {e})")

    def get(self, key: str) -> Source | None:
        return self.sources.get(key)

    # -- the schedule ----------------------------------------------------------------------------------------

    def _choices(self) -> dict:
        try:
            c = self.choices() or {}
            return c if isinstance(c, dict) else {}
        except Exception as e:
            self.log(f"datasources: interval choices not read ({type(e).__name__}: {e}); the defaults")
            return {}

    def choice(self, key: str, choices: dict | None = None) -> str:
        """The interval in force: the admins' choice, else the source's default."""
        src = self.sources[key]
        if not src.managed:
            return ""
        c = (self._choices() if choices is None else choices).get(key)
        return c if c in CHOICES else src.every

    def interval_s(self, key: str, choices: dict | None = None) -> float | None:
        return CHOICES.get(self.choice(key, choices))

    def quiet_at(self, t: float, spread: float = 0.0) -> float:
        """The first moment from t inside the quiet hours (spread 0..1: how far into them)."""
        lo, hi = QUIET_HOURS
        d = datetime.fromtimestamp(t, self.tz)
        if lo <= d.hour < hi:
            return t
        day = d.date() if d.hour < lo else d.date() + timedelta(days=1)
        start = datetime(day.year, day.month, day.day, lo, tzinfo=self.tz).timestamp()
        return start + spread * ((hi - lo) * 3600.0 - 1800.0)

    def next_due(self, key: str, status: dict | None = None, choices: dict | None = None) -> float | None:
        """When the registry runs it next (managed), None when off, unconfigured or not ours."""
        src, st = self.sources[key], self.state[key]
        if not src.managed:
            return None
        ps = _safe(src.status) if status is None else status
        if ps.get("on") is False:
            return None
        iv = self.interval_s(key, choices)
        if iv is None:
            return None
        at = _num(ps.get("at"))
        if at is None and st["ok"]:
            at = st["tried"]                      # read, though the provider keeps no time of its own
        if st["fails"] and st["tried"] is not None:
            wait = min(BACKOFF_S[1], iv, BACKOFF_S[0] * 2 ** (st["fails"] - 1))
            return st["tried"] + wait
        if at is None:
            return st["tried"] or self.started    # never fetched: at once (one at a time, in registration order)
        due = at + iv + st["spread"] * min(iv * JITTER, JITTER_MAX_S)
        # heavy: in the quiet hours (overdue, e.g. after a long stop: the coming ones, not at once mid-shift)
        return self.quiet_at(max(due, self.clock()), st["spread"]) if src.quiet and iv >= CHOICES["daily"] else due

    def due(self, now: float | None = None) -> list[str]:
        now = self.clock() if now is None else now
        choices = self._choices()
        out = []
        for key, src in list(self.sources.items()):
            if not src.managed or self.state[key]["running"]:
                continue
            nd = self.next_due(key, None, choices)
            if nd is not None and now >= nd:
                out.append((nd, self.state[key]["order"], key))
        return [k for _, _, k in sorted(out)]

    def soonest(self) -> float | None:
        """The next time anything managed is due (the scheduler sleeps until then, at most TICK_S)."""
        choices = self._choices()
        ds = [nd for k, s in list(self.sources.items()) if s.managed and not self.state[k]["running"]
              and (nd := self.next_due(k, None, choices)) is not None]
        return min(ds) if ds else None

    def tick(self) -> int:
        """Run what is due, one at a time (gentle on every source). The number run."""
        n = 0
        for key in self.due():
            if self._stop.is_set():
                break
            if self._begin(key, "schedule", ""):
                self._run(key)
                n += 1
        return n

    def start(self) -> DataSources:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self.started = self.clock()
            self._thread = threading.Thread(target=self._loop, name="datasources", daemon=True)
            self._thread.start()
            managed = [k for k, s in self.sources.items() if s.managed]
            self.log(f"datasources: {len(self.sources)} data sources, {len(managed)} on the registry's schedule")
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # pragma: no cover - never let the scheduler die
                self.log(f"datasources: {type(e).__name__}: {e}")
            nxt = self.soonest()
            self._stop.wait(TICK_S if nxt is None else min(TICK_S, max(1.0, nxt - self.clock())))

    # -- running one -----------------------------------------------------------------------------------------

    def _begin(self, key: str, why: str, by: str) -> bool:
        with self._lock:
            st = self.state[key]
            if st["running"]:
                return False
            now = self.clock()
            st.update(running=why, started=now, by=by, why=why)
            if why == "manual":
                st["manual"] = now
        return True

    def _run(self, key: str) -> None:
        src, st = self.sources[key], self.state[key]
        try:
            ok = bool(src.refresh())
            err = "" if ok else (str(_safe(src.status).get("error") or "") or "the source gave nothing")
        except Exception as e:  # the provider's fetch should never raise; if it does, it is this row's error
            ok, err = False, f"{type(e).__name__}: {e}"
        now = self.clock()
        with self._lock:
            st.update(running=None, finished=now, ok=ok, error=err[:300], tried=now, spread=self.rng())
            st["fails"] = 0 if ok else st["fails"] + 1
        self._hold(src)                           # the provider's fetch set its own next: hold it again
        self.log(f"datasources: {src.name}: " + ("refreshed" if ok else f"not refreshed ({err[:160]})")
                 + (f" (by {st['by']})" if st["by"] else f" ({st['why']})"))

    def refresh(self, key: str, by: str = "") -> dict:
        """Refresh now (an admin's button): starts it off the request thread; the row as it is then.
        KeyError: no such source; TooSoon: refused (why in the message)."""
        src = self.sources.get(key)
        if src is None:
            raise KeyError(key)
        if src.refresh is None:
            raise TooSoon(f"{src.name} can't be refreshed from here" + (f": {src.note}" if src.note else ""))
        now = self.clock()
        st = self.state[key]
        if st["running"]:
            raise TooSoon(f"{src.name} is refreshing now")
        last = st["manual"]
        if last is not None and now - last < src.min_gap_s:
            again = last + src.min_gap_s
            raise TooSoon(f"{src.name} was refreshed {_dur(now - last)} ago: once every {_dur(src.min_gap_s)} at most "
                          f"(again from {datetime.fromtimestamp(again, self.tz):%H:%M:%S})")
        why = ""
        if src.blocked is not None:
            try:
                why = src.blocked() or ""
            except Exception:
                why = ""
        if why:
            raise TooSoon(f"{src.name}: {why}")
        if not self._begin(key, "manual", by):
            raise TooSoon(f"{src.name} is refreshing now")
        self.spawn(lambda: self._run(key), f"datasource-{key}")
        return self.row(key)

    # -- what the page gets ----------------------------------------------------------------------------------

    def row(self, key: str, choices: dict | None = None) -> dict:
        src, st = self.sources[key], self.state[key]
        ps = _safe(src.status)
        choices = self._choices() if choices is None else choices
        managed = src.managed
        nd = self.next_due(key, ps, choices) if managed else _num(ps.get("next"))
        if nd is not None and not managed:
            nd = max(nd, self.clock())            # the provider's 0 / a past time: due now
        err = str(ps.get("error") or "") or (st["error"] if st["ok"] is False else "")
        on = ps.get("on") is not False
        return {"key": key, "name": src.name, "group": src.group, "source": src.source, "link": src.link,
                "attribution": src.attribution, "note": str(ps.get("note") or src.note or ""),
                "on": on, "why": str(ps.get("why") or "") if not on else "",
                "last_fetched": _num(ps.get("at")), "next_due": nd if on else None,
                "items": ps.get("n") if isinstance(ps.get("n"), int) else None,
                "bytes": ps.get("bytes") if isinstance(ps.get("bytes"), int) and ps.get("bytes") else None,
                "ok": not err, "error": err[:300],
                "schedule": "managed" if managed else ("builtin" if src.builtin else "none"),
                "builtin": src.builtin, "every": self.choice(key, choices) if managed else "",
                "default_every": src.every if managed else "",
                "quiet": src.quiet and managed and (self.interval_s(key, choices) or 0) >= CHOICES["daily"],
                "refreshable": src.refresh is not None and on, "action": src.action, "min_gap_s": src.min_gap_s,
                "running": bool(st["running"]), "started": st["started"] if st["running"] else None,
                "last_run": {"at": st["finished"], "ok": st["ok"], "error": st["error"], "by": st["by"], "why": st["why"]}
                if st["finished"] else None,
                "manual_at": st["manual"], "fails": st["fails"]}

    def public(self) -> dict:
        choices = self._choices()
        rows = [self.row(k, choices) for k in list(self.sources)]
        rev = hashlib.sha1(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:12]
        return {"rev": rev, "groups": list(GROUPS), "choices": list(CHOICES), "quiet_hours": list(QUIET_HOURS),
                "now": self.clock(), "sources": rows}


def _dur(s: float) -> str:
    s = max(0, int(round(s)))
    if s < 90:
        return f"{s} s"
    if s < 3600:
        return f"{round(s / 60)} min"
    return f"{s // 3600} h" if s % 3600 == 0 else f"{s / 3600:.1f} h"


# ---- the adapters: each provider as it is (duck-typed, nothing imported), its own refresh path ---------------


def _resting(st: dict, clock: Callable[[], float]) -> str:
    """The provider's own backoff after the source refused or rate-limited us: wait for it."""
    err = str(st.get("error") or "")
    nxt = st.get("next") or 0.0
    if err and ("rate limit" in err or "refused" in err) and nxt != HELD and clock() < nxt:
        return f"resting after {err[:120]}; try again after {_dur(nxt - clock())}"
    return ""


def _hold(st: dict) -> Callable[[], None]:
    return lambda: st.__setitem__("next", HELD)


def gis_sources(gis) -> list[Source]:
    """The county GIS layers (sizeup/gis.py): one row per layer, on the layer's own cadence ([gis] layers' refresh:
    the county updates these rarely), in the quiet hours; Refresh now refetches the layer (Gis.refresh)."""
    from sizeup import gis as g
    out = []
    for key, layer in gis.layers.items():
        st = gis.status[key]

        def status(key=key, st=st) -> dict:
            if not gis.configured:
                return {"on": False, "why": "County GIS off ([gis])"}
            return {"at": st["at"], "error": st["error"], "n": st["n"], "bytes": st["bytes"]}
        out.append(Source(f"gis:{key}", f"{layer.label}", "Map layers", g.SOURCE, g.LINK_URL, g.ATTRIBUTION, status,
                          refresh=lambda key=key: gis.refresh(key), every=layer.refresh if layer.refresh in CHOICES else "monthly",
                          hold=_hold(st), quiet=True, min_gap_s=600.0 if layer.tiled else MANUAL_GAP_S,
                          note=f"County GIS · {layer.group}"))
    return out


def roads_sources(roads) -> list[Source]:
    """The highway feeds (sizeup/roads.py): each provider's camera list on the registry's schedule (daily: it
    rarely changes; was the Thruway's hourly), its events and signs on the provider's own (built in)."""
    from sizeup import roads as r
    out = []
    for p in roads.providers:
        group_name = {"511ny": "511NY", "thruway": "Thruway", "county": "County DPW"}.get(p.name, p.short)
        for feed in r.FEEDS:
            if not p.feed_on(feed):
                continue
            st = p.status[feed]

            def status(p=p, feed=feed, st=st) -> dict:
                return {"at": st["at"], "error": st["error"], "n": st["n"], "next": st["next"]}

            def refresh(p=p, feed=feed) -> bool:
                ok = roads.refresh(feed, p)
                if ok and feed == "cameras":
                    roads._swept = 0.0                       # a new camera list: look at them now (Roads._run)
                return ok
            managed = feed == "cameras" and p.name != "511ny"     # 511NY's: as its access approval and [roads] set them
            what = {"cameras": "cameras (the list)", "events": "incidents and closures", "signs": "message signs"}[feed]
            out.append(Source(f"roads:{p.name}:{feed}", f"{group_name} {what}", "Cameras", p.label, p.link,
                              r.ATTRIBUTION if p.name == "511ny" else f"Data: {p.label}", status, refresh,
                              every="daily" if managed else "", hold=_hold(st) if managed else None,
                              builtin="" if managed else f"every {_dur(p.every[feed])}",
                              blocked=lambda st=st: _resting(st, roads.clock)))
    return out


def trains_sources(trains) -> list[Source]:
    """Metro-North (sizeup/trains.py): the static GTFS (stations, line; the MTA updates it about weekly) daily in the
    quiet hours, on the registry's schedule; the realtime positions on the provider's own while someone looks."""
    from sizeup import trains as t

    def off() -> dict | None:
        return None if trains.configured else {"on": False, "why": trains.why_not()}

    def static() -> dict:
        m = trains.model
        return off() or {"at": trains.st["at"], "error": trains.st["error"],
                         "n": len(m.area_stations) if m is not None else None,
                         "note": f"{len(m.area_stations)} New Haven Line stations in our area" if m is not None else ""}

    def rt() -> dict:
        return off() or {"at": trains.rt["at"], "error": trains.rt["error"], "n": trains.rt["n"],
                         "next": trains.rt["next"] if trains.clock() - trains.asked <= t.DEMAND_S else None}
    return [
        Source("trains:static", "Metro-North stations and line (GTFS)", "Transit", t.SOURCE, t.LINK_URL, f"Data: {t.SOURCE}",
               static, trains.refresh_static, every="daily", hold=_hold(trains.st), quiet=True, min_gap_s=600.0,
               blocked=lambda: _resting(trains.st, trains.clock)),
        Source("trains:rt", "Metro-North live positions (GTFS-realtime)", "Transit", t.SOURCE, t.LINK_URL, f"Data: {t.SOURCE}",
               rt, trains.refresh_rt, builtin=f"every {trains.every_s:g} s while the Trains layer or an event near the line is open",
               blocked=lambda: _resting(trains.rt, trains.clock)),
    ]


def weather_sources(weather) -> list[Source]:
    """NWS and NOAA (sizeup/weather.py): all on the provider's own schedule (alerts every 2 min, conditions and the
    gauge every 10, the tide table every 6 h, radar tiles on request); Refresh now reads that feed at once."""
    from sizeup import weather as w
    names = {"alerts": ("NWS alerts", "National Weather Service", w.NWS_LINK),
             "conditions": (f"Current conditions ({weather.station})", "National Weather Service", w.NWS_LINK),
             "tides": (f"Water level ({weather.gauge_station[1]})", "NOAA CO-OPS", w.COOPS_LINK.format(id=weather.gauge_station[0])),
             "predictions": (f"Tide predictions ({weather.tide_station[1]})", "NOAA CO-OPS", w.COOPS_LINK.format(id=weather.tide_station[0]))}
    out = []
    for feed in w.FEEDS:
        st = weather.status[feed]
        name, who, link = names[feed]

        def status(st=st, feed=feed) -> dict:
            if not weather.enabled:
                return {"on": False, "why": "Weather off ([weather])"}
            n = {"alerts": len(weather.alerts)}.get(feed)
            return {"at": st["at"], "error": st["error"], "next": st["next"], "n": n}
        out.append(Source(f"weather:{feed}", name, "Weather", who, link, f"Data: {who}", status,
                          lambda feed=feed: weather.refresh(feed), builtin=f"every {_dur(w.EVERY_S[feed])}",
                          blocked=lambda st=st: _resting(st, weather.clock)))

    def radar() -> dict:
        if not (weather.enabled and weather.radar_on):
            return {"on": False, "why": "Radar off ([weather] radar)"}
        fs = weather.frames_status
        with weather._tile_lock:
            tiles = list(weather._tiles.values())
        return {"at": fs["at"], "error": fs["error"], "n": len(tiles), "bytes": sum(len(x["body"]) for x in tiles),
                "note": f"{len(tiles)} tiles kept, {len(weather.frames)} animation frames"}

    def radar_refresh() -> bool:
        with weather._tile_lock:                  # the cached tiles go: the maps ask afresh
            weather._tiles.clear()
        weather.frames_status["next"] = 0.0
        out = weather.radar_frames()
        return not out.get("error")
    out.append(Source("weather:radar", "Radar (MRMS)", "Weather", "NOAA / NWS", w.RADAR_LINK, "NOAA/NWS MRMS radar", radar,
                      radar_refresh, builtin=f"tiles on request (kept {_dur(w.RADAR_NOW_TTL_S)}), frames every {_dur(w.RADAR_FRAMES_S)}"))
    return out


def aircraft_sources(aircraft) -> list[Source]:
    """Aircraft near HPN (sizeup/aircraft.py): the live feeds on the provider's own schedule (while the layer is open,
    the emergency watch otherwise); the FAA registry (~70 MB) weekly in the quiet hours, at most hourly by hand."""
    from sizeup import aircraft as a
    reg = aircraft.registry

    def live() -> dict:
        if not aircraft.configured:
            return {"on": False, "why": aircraft.why_not()}
        fs = [f.st for f in aircraft.feeds]
        why = "; ".join(f"{a.FEEDS[f.key]['label']}: {f.st['why']}" for f in aircraft.feeds if f.st.get("why"))
        nxt = [s["next"] for s in fs if s.get("next")]
        return {"at": aircraft.st["fetched"], "error": why or aircraft.st["error"], "n": len(aircraft.view),
                "next": min(nxt) if nxt and (aircraft.demanded() or aircraft.watching()) else None,
                "note": " + ".join(a.FEEDS[f.key]["label"] for f in aircraft.feeds)}

    def registry() -> dict:
        if not reg.url:
            return {"on": False, "why": "FAA registry off ([aircraft] registry)"}
        size = reg.path.stat().st_size if reg.path and reg.path.is_file() else None
        return {"at": reg.st["at"], "error": reg.st["error"], "n": reg.st["n"], "bytes": size,
                "note": "N-number, make, model and year only (never an owner)"}
    watch = f", the emergency watch every {a.WATCH_S:g} s otherwise" if aircraft.watching() else ""
    return [
        Source("aircraft:live", "Aircraft positions (ADS-B)", "Aircraft", "adsb.lol / adsb.fi", a.FEEDS["adsblol"]["link"],
               "adsb.lol (ODbL) + adsb.fi open data", live, aircraft.refresh,
               builtin=f"every {a.PRIMARY_S:g} s while the Aircraft layer is open{watch}"),
        Source("aircraft:registry", "FAA aircraft registry", "Aircraft", "FAA (public domain)", "https://registry.faa.gov/",
               a.REGISTRY_CREDIT, registry, reg.refresh, every="weekly", hold=_hold(reg.st), quiet=True, min_gap_s=HEAVY_GAP_S),
    ]


def airport_source() -> Source:
    """The Airport layer's file (data/khpn_airport.geojson, built offline by tools/fetch_airport.py from files in
    data/airport/: nothing to fetch at run time). Reload reads it again from disk (sizeup/cad/airport.py layer())."""
    from sizeup.cad import airport

    def status() -> dict:
        p = airport.LAYER_FILE
        if not p.is_file():
            return {"on": False, "why": f"{p.name} not there (tools/fetch_airport.py)"}
        d = airport.layer()
        return {"at": p.stat().st_mtime, "n": len(d.get("features") or []), "bytes": p.stat().st_size,
                "error": "" if d else f"{p.name} not read",
                "note": "Built from files in data/airport/ (OSM, FAA): nothing to fetch while running"}

    def reload() -> bool:
        airport._cache = None
        return bool(airport.layer())
    return Source("airport", "Airport (HPN) layer", "Map layers", "OpenStreetMap, FAA", "https://www.openstreetmap.org/",
                  "© OpenStreetMap contributors; FAA", status, reload, every="off", action="Reload")


def gazetteer_source(gaz) -> Source:
    """The address and place search (data/gazetteer.sqlite): built offline (tools/fetch_addresses.py,
    tools/fetch_places.py, a few hundred MB of downloads) and shipped with a deploy. Shown, never fetched here."""
    def status() -> dict:
        if gaz is None:
            return {"on": False, "why": "the gazetteer isn't loaded (data/gazetteer.sqlite)"}
        p = Path(gaz.path)
        meta = getattr(gaz, "meta", {}) or {}
        built = meta.get("places_built") or meta.get("built") or ""
        try:
            at = datetime.fromisoformat(built).timestamp() if built else p.stat().st_mtime
        except ValueError:
            at = p.stat().st_mtime if p.is_file() else None
        try:
            n = len(json.loads(meta.get("sources") or "[]"))
        except ValueError:
            n = None
        return {"at": at, "bytes": p.stat().st_size if p.is_file() else None,
                "note": (f"{n} sources; " if n else "") + "rebuilt offline (tools/fetch_addresses.py, tools/fetch_places.py) "
                                                         "and shipped with a deploy"}
    return Source("gazetteer", "Addresses and places (gazetteer)", "Map layers", "NYS, US Census, NYSDOT, OSM", "",
                  "NYS Address Points, US Census TIGER, NYSDOT, © OpenStreetMap contributors", status, None,
                  note="built offline: rebuilt with tools/fetch_places.py and a deploy")


def tones_source(path: Path | None, codes: Callable[[], list], reload: Callable[[], bool] | None) -> Source:
    """The tone list (data/westchester_tones.csv: the RadioReference wiki's Westchester tone outs, copied by hand, each
    row with the date it was fetched). Reload reads the file again and re-applies it (Settings > Radios > Tones' edits
    stay on top). The wiki isn't fetched from here."""
    def status() -> dict:
        if path is None or not path.is_file():
            return {"on": False, "why": "no tone list file ([radio] tones_file)"}
        rows = [c for c in codes() if getattr(c, "source", "") != "config"]
        fetched = sorted(str(getattr(c, "fetched", "") or "") for c in rows if getattr(c, "fetched", ""))
        try:
            at = datetime.fromisoformat(fetched[-1]).replace(tzinfo=TZ).timestamp() if fetched else path.stat().st_mtime
        except ValueError:
            at = path.stat().st_mtime
        return {"at": at, "n": len(rows), "bytes": path.stat().st_size,
                "note": f"{path.name}: copied from the wiki by hand; reloaded from the file here"}
    return Source("tones", "Tone list (Westchester tone outs)", "Radio", "RadioReference wiki",
                  "https://wiki.radioreference.com/index.php/Westchester_County_(NY)_Fire_Tone_Outs", "RadioReference wiki",
                  status, reload, every="off", action="Reload")


def build(app, log: Callable[[str], None] = print, choices: Callable[[], dict] | None = None) -> DataSources:
    """Every pull the app has (sizeup/app.py), registered. A new provider: one more register() here."""
    ds = DataSources(choices or (lambda: {}), log=log)
    adders = (
        lambda: gis_sources(app.gis),
        lambda: [airport_source()],
        lambda: [gazetteer_source(getattr(app, "gaz", None))],
        lambda: roads_sources(app.roads),
        lambda: trains_sources(app.trains),
        lambda: weather_sources(app.weather),
        lambda: aircraft_sources(app.aircraft),
        lambda: [tones_source(*app.tone_list_source())] if hasattr(app, "tone_list_source") else [],
    )
    for add in adders:
        try:
            for src in add():
                ds.register(src)
        except Exception as e:  # one provider's trouble never takes the others' rows away
            log(f"datasources: not registered ({type(e).__name__}: {e})")
    return ds
