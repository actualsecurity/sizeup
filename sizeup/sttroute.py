"""Which engine transcribes a clip: the engine selector of Settings > Radios > Channels (#875) and its no-slowdown
guards (#878). The pool (sizeup/sttpool.py) holds the queue and the leases, the hub (sizeup/cloud/hub.py) routes the
clips; this module is their shared vocabulary and the parts with no I/O.

A channel's engine (sizeup/channels.py ENGINES; the stored value, then what the dropdown shows):

    auto        Auto · priority: an idle GPU worker, then Amazon Transcribe (its budget and reserve rules), then the
                station's whisper. For the pages, Fire/EMS main talkgroups, firegrounds, police alert talkgroups and
                the airband. The station skips its whisper and hands the clip to the hub at once (unless the hub said
                no GPU worker is ready and AWS can't take it: then its own whisper at once, no round trip).
    auto_bg     Auto · background (#896): the stations' whisper first (its own, then any station in the pool that can
                start it within its class's budget: BG_BUDGET_S), a GPU worker as overflow (idle, no priority work on
                it, none in the last BG_QUIET_S), AWS last within its budget, else back to its own station's queue.
                Never dropped. The default of every context or neighbour talkgroup.
    pin:<id>    one engine by its real id: a GPU worker ("pin:gpu-pc"), another station's whisper ("pin:station:<id>")
                or AWS ("pin:aws"). Tried first, then Auto · priority's order (a GPU worker, AWS, the station's
                whisper). Pinned AWS is AWS: a GPU worker only once AWS can't take the clip (its budget out, the
                breaker), never ahead of it.
    local       pinned to the channel's own station (its whisper): today's "Local", unchanged.
    aws         never stored (#896): only what a station from before the selector is sent; the hub routes any "aws" it
                meets as "pin:aws".
    both        Compare: the station's whisper and AWS on every clip (today's "Both", unchanged).

Migration (#896, the admin: "two places shouldn't set what goes where"): on the CAD of record's first start with it,
once and audited (Editor.migrate_engines), a stored "aws" becomes "pin:aws", and the [stt] aws_channels default is
"pin:aws". "local" already is the channel's own station pinned and "both" Compare. A station from before the selector
never sees a new value (legacy(): the hub sends it the nearest old one; the hub still routes the clip by its own choice).

The channel's engine is the only thing that decides where its audio goes: the pool's per-class switches are gone, and
no worker-side class opt-out replaces them. A clip a station hands over when its whisper is behind goes its channel's
way (a pinned station's: Auto · priority's order after it); the catch-up goes Auto · background's way (a GPU with no
priority work, the station's whisper, AWS last for another P25 system's talkgroups within the context budget). The
pool's own on/off switch holds for all of it.

Every transcript the hub routes records the way it went (Route): "auto·priority: gpu-pc → AWS (gpu-pc: gaming)".

Guard (#878): a GPU worker whose live latency (transmission end -> text, priority clips) or failure rate (its priority
jobs' leases lost, handed back, results refused) crosses GUARD_P50_S / GUARD_FAIL_RATE over GUARD_WINDOW_S is skipped by
every Auto and pinned channel (they go to AWS and say why) until GUARD_RECOVER_N probes in a row come back quick: after
GUARD_HOLD_S (doubling on each trip within GUARD_RESET_S), one per GUARD_PROBE_S, each a shadow copy of a priority clip
(never a live clip on a known-slow worker). Its state survives a restart (the hub keeps it: EngineBook.state/restore).
Health shows each engine's live latency and the guard's state.
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

# Auto · background (#896, the admin: "stations first, if overflow then the GPU, AWS last"): a clip goes to a station's
# whisper that can START it within its class's budget (estimated wait: its queue x its measured time per clip), the
# clip's own station first; none can: an idle GPU at once (overflow); none: AWS within its budget; else back to its own
# station's queue. Never dropped. A constant for now (Settings later).
BG_BUDGET_S = 90.0        # LOW (context talkgroups), RERUN, CATCHUP
BG_BUDGET_FAST_S = 60.0   # context alert classes (CONTEXT, CONTEXT_FALLBACK) ...
BG_BUDGET_LIVE_S = 30.0   # ... and anything of a live class on a background channel (a neighbour's main, an emergency)
BG_QUIET_S = 3.0          # a background clip goes to a GPU only this long after the last priority clip came in
BG_GPU_MAX_S = 30.0       # ... and only one this short (the GPU is back for priority work in a moment)

GUARD_WINDOW_S = 600.0    # a GPU worker's live samples looked at over this long
GUARD_MIN_N = 4           # ... at least this many before it can trip
GUARD_P50_S = 5.0         # its median transmission end -> text over this (sttruns.TARGET_P50_S): tripped
GUARD_FAIL_RATE = 0.34    # ... or this share of its priority jobs failed (at least GUARD_FAIL_MIN of them)
GUARD_FAIL_MIN = 2
GUARD_HOLD_S = 300.0      # tripped: no priority clip for this long (doubling each trip within GUARD_RESET_S, up to
GUARD_HOLD_MAX_S = 3600.0  # GUARD_HOLD_MAX_S: a borderline worker never cycles), then ...
GUARD_RESET_S = 3600.0
GUARD_PROBE_S = 60.0      # ... one probe this often: a shadow copy of a priority clip (its text never used; the clip
GUARD_PROBE_RUN_S = 3.0   # itself goes its own way), decoded within this (claim -> text: sttpool PREALERT_MAX_SERVICE_S)
GUARD_RECOVER_N = 3       # ... and this many good ones in a row: back
LATENCY_WINDOW_S = 7200.0  # Health's live latency per engine


def bg_budget(cls) -> float:
    """How long an Auto · background clip of this class may wait to START on a station's whisper (seconds)."""
    from sizeup.stt import CONTEXT, CONTEXT_FALLBACK, HIGH, LIVE, URGENT
    if cls in (URGENT, HIGH, LIVE):
        return BG_BUDGET_LIVE_S
    if cls in (CONTEXT, CONTEXT_FALLBACK):
        return BG_BUDGET_FAST_S
    return BG_BUDGET_S


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
        if not self.hops and self.mode.lower() in (final.lower(), f"pinned {final.lower()}"):
            return self.mode                                  # ("pinned AWS", not "pinned AWS: AWS")
        names = [e for e, _w in self.hops if e != final] + [final]
        whys = [f"{e}: {w}" if w and not w.startswith(e) else w for e, w in self.hops if w and e != final]
        return (f"{self.mode}: " + " → ".join(names) + (f" ({'; '.join(whys)})" if whys else ""))[:300]


class Routes:
    """Each clip's Route while its text is on the way (the hub; memory only, at most MAX, oldest out first)."""

    MAX = 5000

    def __init__(self):
        self._d: dict[str, Route] = {}
        self._chosen: dict[str, str] = {}         # clip id -> the engine its text of record is expected from now
        self._lock = threading.Lock()

    def choose(self, cid: str, engine: str) -> None:
        """The engine this clip's route sends it to now ("GPU", "gpu-pc", "AWS", "station"): its text is the text of
        record; another engine's earlier text is provisional (the hook for a station's early text, #896)."""
        with self._lock:
            self._chosen.pop(cid, None)
            self._chosen[cid] = str(engine or "")[:40]
            while len(self._chosen) > self.MAX:
                self._chosen.pop(next(iter(self._chosen)))

    def chosen(self, cid: str) -> str:
        """The engine the route chose for this clip ("" for a clip the hub didn't route: the station's own text)."""
        with self._lock:
            return self._chosen.get(cid, "")

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

    def __init__(self, clock: Callable[[], float] = time.time, log: Callable[[str], None] = print,
                 on_change: Callable[[dict], None] | None = None):
        """on_change(state): the guard tripped, held again or cleared (the hub keeps it across a restart)."""
        self.clock, self.log, self.on_change = clock, log, on_change
        self._rows: dict[str, deque] = {}          # engine -> (at, latency_s | None, ok, priority)
        self._trip: dict[str, dict] = {}           # GPU worker -> {at, why, probe_at, good, hold}
        self._trips: dict[str, list] = {}          # GPU worker -> [trips in a row, the last one's time] (the backoff)
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
            changed = self._judge(engine, at, lat, bool(ok), probe) if gpu and (priority or probe) else False
        if changed:
            self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self.state())
            except Exception as e:  # noqa: BLE001 - kept in memory all the same
                self.log(f"stt guard: not kept: {type(e).__name__}: {e}")

    def state(self) -> dict:
        """What survives a restart: the tripped workers and the backoff."""
        with self._lock:
            return {"trip": {w: dict(t) for w, t in self._trip.items()},
                    "trips": {w: list(v) for w, v in self._trips.items()}}

    def restore(self, state) -> None:
        """A state from before a restart (state()); anything unreadable: none."""
        if not isinstance(state, dict):
            return
        with self._lock:
            for w, t in (state.get("trip") or {}).items() if isinstance(state.get("trip"), dict) else ():
                if isinstance(t, dict) and all(isinstance(t.get(k), (int, float)) for k in ("at", "probe_at", "good", "hold")):
                    self._trip[str(w)] = {"at": float(t["at"]), "why": str(t.get("why") or "")[:80],
                                          "probe_at": float(t["probe_at"]), "good": int(t["good"]), "hold": float(t["hold"])}
            for w, v in (state.get("trips") or {}).items() if isinstance(state.get("trips"), dict) else ():
                if isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v):
                    self._trips[str(w)] = [int(v[0]), float(v[1])]

    def _judge(self, wid: str, now: float, lat: float | None, ok: bool, probe: bool = False) -> bool:
        """(lock held) A priority sample on a GPU worker: trip the guard; a probe's (lat: its decode, claim -> text)
        counts towards its recovery. True when the guard's state changed."""
        t = self._trip.get(wid)
        if t is not None:
            if not probe:                            # (a clip in flight when it tripped, any other: no probe's answer)
                return False
            if ok and lat is not None and lat <= GUARD_PROBE_RUN_S:
                t["good"] += 1
                if t["good"] >= GUARD_RECOVER_N:
                    self._trip.pop(wid, None)
                    self._cleared[wid] = now
                    self.log(f"stt guard: {wid}: {GUARD_RECOVER_N} probes in time: priority clips go to it again")
                    return True
                return False
            t["good"], t["at"] = 0, now              # (a bad probe: held again, its hold as it was)
            t["why"] = "probe failed" if not ok else f"probe {lat:.1f} s"
            return True
        if probe:
            return False
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
        if not why:
            return False
        n, last = self._trips.get(wid, [0, 0.0])
        n = 1 if now - last > GUARD_RESET_S else n + 1
        self._trips[wid] = [n, now]
        hold = min(GUARD_HOLD_MAX_S, GUARD_HOLD_S * 2 ** (n - 1))
        self._trip[wid] = {"at": now, "why": why, "probe_at": 0.0, "good": 0, "hold": hold}
        self.log(f"stt guard: {wid}: {why} over {GUARD_WINDOW_S / 60:.0f} min: its priority clips go to AWS "
                 f"for {hold / 60:.0f} min at least, until it is quick again")
        return True

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
            return t is not None and now - t["at"] >= t.get("hold", GUARD_HOLD_S) and now - t["probe_at"] >= GUARD_PROBE_S

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
