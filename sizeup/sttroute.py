"""Which engine transcribes a clip: the engine selector of Settings > Radios > Channels (#875) and its no-slowdown
guards (#878). The pool (sizeup/sttpool.py) holds the queue and the leases, the hub (sizeup/cloud/hub.py) routes the
clips; this module is their shared vocabulary and the parts with no I/O.

A channel's engine (sizeup/channels.py ENGINES; the stored value, then what the dropdown shows):

    auto        Auto · priority: an idle GPU worker, then Amazon Transcribe (its budget and reserve rules), then the
                station's whisper. For the pages, Fire/EMS main talkgroups, firegrounds, police alert talkgroups and
                the airband. The station skips its whisper and hands the clip to the hub at once (unless the hub said
                no GPU worker is ready and AWS can't take it: then its own whisper at once, no round trip).
    auto_bg     Auto · background: a GPU worker only while no priority work is queued or held on it (and none came in
                the last BG_QUIET_S), else the station's whisper (an idle one elsewhere in the cluster when it is behind,
                #639 phase 2), AWS last within the context budget (the catch-up), never dropped.
    pin:<id>    one engine by its real id: a GPU worker ("pin:gpu-pc"), another station's whisper ("pin:station:<id>")
                or AWS ("pin:aws"). Tried first, then Auto · priority's order (a GPU worker, AWS, the station's
                whisper). Pinned AWS is AWS: a GPU worker only once AWS can't take the clip (its budget out, the
                breaker), never ahead of it.
    local       pinned to the channel's own station (its whisper): today's "Local", unchanged.
    aws         today's "AWS", unchanged (legacy): Amazon Transcribe, with a GPU worker first wherever the pool's
                class switch for the clip's class is on (#639/#651), the station's whisper without AWS.
    both        Compare: the station's whisper and AWS on every clip (today's "Both", unchanged).

Migration: nothing is rewritten and nothing changes live. "local" is the pinned station, "both" is Compare, and a
channel on "aws" keeps today's behaviour exactly (GPU first only for the classes ticked in the pool's switches); it
is shown as "AWS · GPU by class (legacy)" until an admin picks pinned AWS (AWS only), Auto or another engine. A station
from before the selector never sees a new value (legacy(): the hub sends it the nearest old one; the hub still routes
the clip by its own choice).

Precedence: a channel's own engine (Auto, pinned) wins over the pool's per-class switches (Settings > Station > STT
workers); those still decide, per class, what the pool takes for everything else: legacy "aws" channels' GPU-first, the
clips a station hands over when its whisper is behind (#639 phase 2) and the catch-up. The pool's own on/off switch
holds for all of it.

Every transcript the hub routes records the way it went (Route): "auto·priority: gpu-pc → AWS (gpu-pc: gaming)".

Guard (#878): a GPU worker whose live latency (transmission end -> text, priority clips) or failure rate (leases lost,
handed back, results refused) crosses GUARD_P50_S / GUARD_FAIL_RATE over GUARD_WINDOW_S is skipped by every Auto and
pinned channel (they go to AWS and say why) until GUARD_RECOVER_N probe clips in a row, one per GUARD_PROBE_S after
GUARD_HOLD_S, come back within the threshold. Health shows each engine's live latency and the guard's state.
Later (#878): the shadow-evidence summary (a channel's GPU candidates scored against AWS on the same clips) for an
admin switching a channel off AWS; nothing here switches a channel by itself."""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Callable

AUTO, AUTO_BG, PIN = "auto", "auto_bg", "pin:"
LEGACY = ("local", "aws", "both")
STATION_PIN = "station:"
# "pin:gpu-pc" (a GPU worker's id: sizeup/cloud/sttworkers.py ID) or "pin:station:<id>" (a station's id)
PIN_RE = re.compile(r"pin:(?:station:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}|[a-z0-9][a-z0-9_-]{0,39})")
PIN_AWS = "pin:aws"       # Amazon Transcribe and nothing else while it can take the clip (a GPU worker only when it can't)
MODES = {AUTO: "auto·priority", AUTO_BG: "auto·background", "local": "station", "aws": "AWS", "both": "compare"}

BG_QUIET_S = 3.0          # a background clip goes to a GPU only this long after the last priority clip came in
BG_GPU_MAX_S = 30.0       # ... and only one this short (the GPU is back for priority work in a moment)

GUARD_WINDOW_S = 600.0    # a GPU worker's live samples looked at over this long
GUARD_MIN_N = 4           # ... at least this many before it can trip
GUARD_P50_S = 5.0         # its median transmission end -> text over this (sttruns.TARGET_P50_S): tripped
GUARD_FAIL_RATE = 0.34    # ... or this share of its priority jobs failed (at least GUARD_FAIL_MIN of them)
GUARD_FAIL_MIN = 2
GUARD_HOLD_S = 300.0      # tripped: no priority clip for this long, then ...
GUARD_PROBE_S = 60.0      # ... one probe clip this often ...
GUARD_RECOVER_N = 3       # ... and this many good ones in a row: back
LATENCY_WINDOW_S = 7200.0  # Health's live latency per engine


def is_auto(eng: str) -> bool:
    return eng in (AUTO, AUTO_BG)


def is_pin(eng: str) -> bool:
    return isinstance(eng, str) and eng.startswith(PIN)


def is_new(eng: str) -> bool:
    """An engine a station from before the selector doesn't know (channels.check_map would refuse the whole map)."""
    return is_auto(eng) or is_pin(eng)


def hub_aws(eng: str) -> bool:
    """May this channel's clips go to Amazon Transcribe on the hub in their order (AWS, Compare, Auto · priority, a
    pinned engine's fallback): a person's re-run of one goes there, and a context one's AWS is counted (the station's
    word). Auto · background's AWS is the catch-up's alone."""
    return eng in ("aws", "both", AUTO) or is_pin(eng)


def pinned(eng: str) -> str:
    """The id a pinned engine names ("gpu-pc", "station:north"), "" for any other."""
    return eng[len(PIN):] if is_pin(eng) else ""


def valid(eng) -> bool:
    return isinstance(eng, str) and (eng in LEGACY or eng in (AUTO, AUTO_BG) or bool(PIN_RE.fullmatch(eng)))


def strict_aws(eng: str) -> bool:
    return eng == PIN_AWS


def legacy(eng: str) -> str:
    """The nearest engine a station from before the selector knows: Auto · priority and a pinned GPU worker or another
    station are the hub's ("aws": the station hands the clip over, or uses its whisper without AWS, as before), Auto ·
    background is the station's whisper."""
    if eng == AUTO_BG:
        return "local"
    return "aws" if is_new(eng) else eng


def mode_label(eng: str) -> str:
    if is_pin(eng):
        return "pinned AWS" if eng == PIN_AWS else f"pinned {pinned(eng).removeprefix(STATION_PIN)}"
    return MODES.get(eng, eng or "station")


def engine_name(engine: str, worker: str = "", model: str = "") -> str:
    """The short name of the engine that wrote a transcript (sttruns.ENGINES + its worker): "gpu-pc", "AWS", "station
    north" ("station" when the station's whisper doesn't say which)."""
    if engine == "aws":
        return "AWS"
    if engine == "gpu-worker":
        return worker or "GPU"
    if engine == "station-whisper":
        sid = worker.removeprefix(STATION_PIN) if worker else ""
        return f"station {sid}" if sid else "station"
    return engine or "?"


class Route:
    """The way one clip went: the mode its channel's engine is in, and each engine passed over with why. text(final):
    "auto·priority: gpu-pc → AWS (gpu-pc: gaming)"; "auto·priority: AWS (GPU: no worker online)"."""

    __slots__ = ("mode", "hops", "at")

    def __init__(self, mode: str, at: float | None = None):
        self.mode, self.hops, self.at = mode, [], time.time() if at is None else at

    def note(self, engine: str, why: str) -> None:
        engine, why = str(engine or "?")[:40], str(why or "")[:100]
        if self.hops and self.hops[-1][0] == engine:          # (the same engine twice in a row: its last word)
            self.hops[-1] = (engine, why or self.hops[-1][1])
        else:
            self.hops.append((engine, why))
        del self.hops[:-6]

    def text(self, final: str) -> str:
        names = [e for e, _w in self.hops if e != final] + [final]
        whys = [f"{e}: {w}" if w and not w.startswith(e) else w for e, w in self.hops if w and e != final]
        return (f"{self.mode}: " + " → ".join(names) + (f" ({'; '.join(whys)})" if whys else ""))[:300]


class Routes:
    """Each clip's Route while its text is on the way (the hub; memory only, at most MAX, oldest out first)."""

    MAX = 5000

    def __init__(self):
        self._d: dict[str, Route] = {}
        self._lock = threading.Lock()

    def start(self, cid: str, mode: str) -> Route:
        with self._lock:
            r = self._d.get(cid)
            if r is None or r.mode != mode:
                r = self._d[cid] = Route(mode)
                while len(self._d) > self.MAX:
                    self._d.pop(next(iter(self._d)))
            return r

    def note(self, cid: str, engine: str, why: str) -> None:
        with self._lock:
            r = self._d.get(cid)
            if r is not None:
                r.note(engine, why)

    def get(self, cid: str) -> Route | None:
        with self._lock:
            return self._d.get(cid)

    def finish(self, cid: str, final: str) -> str:
        """Its route as text, ending at the engine that wrote its text ("" for a clip with no route here)."""
        with self._lock:
            r = self._d.pop(cid, None)
        return r.text(final) if r is not None else ""


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return round(s[k], 2)


class EngineBook:
    """Live latency and failures per engine ("gpu-pc", "AWS", "station north"), and the guard on the GPU workers.

    add(engine, latency_s, ok, priority): a live transcript (latency: transmission end -> text on the hub) or a failure
    (ok False: no latency). Only priority samples count for the guard; every one counts for Health."""

    def __init__(self, clock: Callable[[], float] = time.time, log: Callable[[str], None] = print):
        self.clock, self.log = clock, log
        self._rows: dict[str, deque] = {}          # engine -> (at, latency_s | None, ok, priority)
        self._trip: dict[str, dict] = {}           # GPU worker -> {at, why, probe_at, good}
        self._cleared: dict[str, float] = {}       # GPU worker -> when it recovered (older samples no longer count)
        self._lock = threading.Lock()

    def add(self, engine: str, latency_s: float | None, ok: bool = True, priority: bool = True,
            gpu: bool = False, at: float | None = None, probe: bool = False) -> None:
        if not engine:
            return
        if latency_s is not None and (not isinstance(latency_s, (int, float)) or latency_s < -30 or latency_s > 86400):
            return
        at = self.clock() if at is None else at
        lat = max(0.0, float(latency_s)) if latency_s is not None else None
        with self._lock:
            q = self._rows.setdefault(engine, deque())
            q.append((at, lat, bool(ok), bool(priority)))
            while q and at - q[0][0] > LATENCY_WINDOW_S:
                q.popleft()
            if gpu and (priority or probe):
                self._judge(engine, at, lat, bool(ok), probe)

    def _judge(self, wid: str, now: float, lat: float | None, ok: bool, probe: bool = False) -> None:
        """(lock held) A priority sample on a GPU worker: trip the guard, or (a probe clip's: the job the hub sent it as
        one) count towards its recovery."""
        t = self._trip.get(wid)
        if t is not None:
            if not probe:                            # (a clip in flight when it tripped, any other: no probe's answer)
                return
            if ok and lat is not None and lat <= GUARD_P50_S:
                t["good"] += 1
                if t["good"] >= GUARD_RECOVER_N:
                    self._trip.pop(wid, None)
                    self._cleared[wid] = now
                    self.log(f"stt guard: {wid}: {GUARD_RECOVER_N} probe clips in time: priority clips go to it again")
            else:
                t["good"], t["at"] = 0, now          # (a bad probe: held again)
                t["why"] = "probe failed" if not ok else f"probe {lat:.1f} s"
            return
        live = [x for x in self._rows.get(wid, ()) if x[3] and now - x[0] <= GUARD_WINDOW_S
                and now - x[0] >= 0 and x[0] >= self._cleared.get(wid, 0.0)]
        if len(live) < GUARD_MIN_N:
            return
        lats = [x[1] for x in live if x[1] is not None]
        fails = sum(1 for x in live if not x[2])
        p50 = _pct(lats, 50)
        why = ""
        if fails >= GUARD_FAIL_MIN and fails / len(live) >= GUARD_FAIL_RATE:
            why = f"{fails} of {len(live)} failed"
        elif p50 is not None and p50 > GUARD_P50_S:
            why = f"p50 {p50:.1f} s"
        if why:
            self._trip[wid] = {"at": now, "why": why, "probe_at": 0.0, "good": 0}
            self.log(f"stt guard: {wid}: {why} over {GUARD_WINDOW_S / 60:.0f} min: its priority clips go to AWS "
                     f"until it is quick again")

    def tripped(self, wid: str, now: float | None = None) -> str:
        """Why this GPU worker is kept off priority clips now ("" for not): its guard is tripped (a probe due or not:
        the hub sends the probe itself, pinned to it: due(), probe())."""
        with self._lock:
            t = self._trip.get(wid)
            return f"latency guard: {t['why']}" if t is not None else ""

    def due(self, wid: str, now: float | None = None) -> bool:
        """This worker is tripped and a probe clip is due now (the hub pins the next priority clip to it)."""
        now = self.clock() if now is None else now
        with self._lock:
            t = self._trip.get(wid)
            return t is not None and now - t["at"] >= GUARD_HOLD_S and now - t["probe_at"] >= GUARD_PROBE_S

    def probe(self, wid: str, now: float | None = None) -> bool:
        """A probe clip went to this tripped worker: the next one waits GUARD_PROBE_S. True when it was one."""
        now = self.clock() if now is None else now
        with self._lock:
            t = self._trip.get(wid)
            if t is None:
                return False
            t["probe_at"] = now
            return True

    def guard(self) -> dict:
        """{worker: {since, why, good}} of the tripped ones."""
        with self._lock:
            return {w: {"since": t["at"], "why": t["why"], "good": t["good"]} for w, t in self._trip.items()}

    def table(self) -> list[dict]:
        """Health: [{engine, n, p50_s, p95_s, failed, guard}], the busiest first."""
        now = self.clock()
        out = []
        with self._lock:
            for eng, q in list(self._rows.items()):
                while q and now - q[0][0] > LATENCY_WINDOW_S:
                    q.popleft()
                if not q:
                    self._rows.pop(eng, None)
                    continue
                lats = [x[1] for x in q if x[1] is not None]
                t = self._trip.get(eng)
                out.append({"engine": eng, "n": len(lats), "p50_s": _pct(lats, 50), "p95_s": _pct(lats, 95),
                            "failed": sum(1 for x in q if not x[2]),
                            "guard": f"tripped: {t['why']}" if t is not None else ""})
        out.sort(key=lambda r: (-r["n"], r["engine"]))
        return out
