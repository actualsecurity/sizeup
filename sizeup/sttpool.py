"""The GPU worker pool (#639): clips the hub hands to speech-to-text workers that connect out to it, with a fast
fallback to the path the clip would have taken without them (Amazon Transcribe, then the station's whisper; a shed
clip back to the catch-up). Opportunistic: a worker may be gaming, paused or gone at any moment, so nothing waits on
one for longer than its class allows, and nothing is ever dropped.

    jobs        one per clip id (the radio log's id, CLIP_ID: the id the station already uses; a P25 call split per
                transmission is one job per transmission), never two for one id at once. In memory on the hub's
                leader: after a restart what was in flight is still "pending" on its row and _aws_recover routes it
    classes     the station's whisper classes (sizeup/stt.py), strict: URGENT, HIGH, LIVE, RERUN, CONTEXT_FALLBACK,
                CONTEXT, LOW, CATCHUP; in a class the oldest first. A catch-up clip never goes ahead of a live one
    ready       a worker that says "online", in a claim now or seen in the last READY_S, not cooling down after
                FAIL_N failures in a row (leases lost, released); and the pool on for that class (Settings). A clip
                whose estimated wait (the work ahead x the median service time / ready workers) is over its class's
                wait is not taken: never slower than the path it would have taken
    budgets     BUDGETS[class] = (wait for a claim, first lease, lease cap from the claim), seconds; each lease
                PER_AUDIO_S more per second of audio. Not claimed in time, a lease run out or past its cap, handed
                back (release), a worker gone: the job's fallback, at once, on the pool's own thread
    engines     a channel's engine choice (#875, sizeup/sttroute.py): forced (Auto · priority, a pinned worker): taken
                while a GPU worker could take it whatever the class switches and however quick it was lately (the
                hub's latency guard decides that: avoid); pin: that worker only; bg (Auto · background, the catch-up):
                after every other job, and on a GPU worker only while it holds nothing, no other job waits and no
                priority clip came in the last BG_QUIET_S (#878: priority work never waits behind it)
    results     complete() from the lease's holder while the lease is live: on_done(job, result). Late (the lease
                gone): refused and counted, never applied
    numbers     done, fallbacks by reason, refused late answers, per worker; queued -> done p50/p90 (stats(), Health)

Nothing here knows HTTP or the CAD: the hub (sizeup/cloud/hub.py) routes clips in and the worker API calls in
(sizeup/cloud/sttworkers.py)."""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from sizeup.stt import CATCHUP, CLASS_NAMES, CONTEXT, CONTEXT_FALLBACK, HIGH, LIVE, LOW, RERUN, URGENT
from sizeup.sttroute import BG_GPU_MAX_S, BG_QUIET_S

# class -> (wait for a claim, first lease, lease cap from the claim), seconds (#639; the pre-alert channels' target:
# transmission end -> text p50 <= 5 s, p95 <= 15 s, so their wait and cap are short)
BUDGETS: dict[float, tuple[float, float, float]] = {
    URGENT: (2.5, 4.0, 8.0), HIGH: (2.5, 4.0, 8.0), CONTEXT_FALLBACK: (2.5, 4.0, 8.0), CONTEXT: (2.5, 4.0, 8.0),
    LIVE: (5.0, 10.0, 20.0), RERUN: (10.0, 15.0, 45.0), LOW: (30.0, 20.0, 60.0), CATCHUP: (120.0, 20.0, 20.0)}
# (a catch-up clip keeps the catch-up's own bounds on any worker: one greedy pass, 20 s at most, sttcatchup's clip cap)
PER_AUDIO_S = 0.25        # each lease (and cap) this much longer per second of audio (never a pre-alert class's: its
                          # deadline is absolute, so a long page can't hold a dead worker's clip for longer)
READY_S = 15.0            # a worker seen this recently (or in a claim now) is ready ...
OFFLINE_S = 30.0          # ... and one not seen this long is offline
LOST_S = 12.0             # a worker holding a lease unheard this long (two heartbeats): its leases fall back at once
MAX_LEASES = 2            # jobs one worker holds at once at most
FAST_WINDOW_S = 1200.0         # a worker's pre-alert times are looked at over this long (older ones forgotten)
PROBE_S = 180.0                # a worker found slow on them gets one pre-alert clip again this long after its last
                               # slow one or try (a probe: how it recovers; a quick answer opens the gate again)
STATION_DEFER_S = 1.0          # a job a GPU worker could take now waits this long before a station's whisper may claim it
NORM_QUIET_S = BG_QUIET_S     # an Auto · normal clip goes to a GPU only this long after the last priority clip came in
BG_STATION_FIRST_S = 2.0       # ... but background work (#896) the other way round: a station's whisper first this long
                               # (the order: a GPU worker, an idle station's whisper, then AWS; #639 phase 2)
MODEL_DEFER_S = 1.0            # a job for a model (sizeup/sttmodels.py) that an idle worker has loaded waits this long
                               # for that worker before another GPU worker takes it on its own model (never longer: a
                               # model is a preference, never a reason to wait)
KINDS = ("gpu", "station")
PREALERT_MAX_SERVICE_S = 3.0   # a pre-alert clip goes to the pool only to an idle worker (in a claim now) whose recent
                               # claim -> text on these classes is this quick (AWS answers them in seconds)
FAIL_N = 3                # leases lost or handed back in a row: the worker cools down ...
COOLDOWN_S = 60.0         # ... this long (no new jobs), doubling each time it happens again before a job is done ...
COOLDOWN_MAX_S = 1800.0   # ... up to this
SERVICE_DEFAULT_S = 2.0   # a job's claim -> done before any was measured (the estimate of the wait)
CLAIM_WAIT_MAX_S = 25.0   # a claim's long poll at most
TICK_S = 0.25             # how often waits and leases are looked at
WINDOW_S = 7200.0         # done jobs kept for the numbers (Health), seconds
STATES = ("online", "gaming", "paused")
REASONS = ("unclaimed", "lease", "released", "no_worker", "error", "discarded")   # why a job fell back (discarded:
# its text was a repetition loop or its context read back, sizeup/sttmodels.py)
CLASSES = tuple(BUDGETS)
# the pre-alert classes: an absolute deadline (no per-audio extension), only an idle quick worker, one greedy pass
# (whisper-server's temperature_inc 0, as the station's WhisperClient fast=True), so a hard clip is never decoded five
# more times past its lease, and back at once when their worker starts gaming or pauses
FAST_CLASSES = (URGENT, HIGH, CONTEXT_FALLBACK, CONTEXT)


# class -> how long after its job was queued (its event applied) a worker's audio request may still wait for the
# station's own STT input (#671's stt-src, 16 kHz) before it gets the 8 kHz listening clip (#678). It comes on its own
# lane, under the events: ~0.2-0.5 s after its event once the lane is woken by the events' ack (sizeup/cloud/station.py),
# ~2 s before that fix. A pre-alert clip never waits long for the better input; a re-run or catch-up clip is old (its
# input is here already, or never comes)
SRC_WAIT_S: dict[float, float] = {
    URGENT: 0.75, HIGH: 0.75, CONTEXT_FALLBACK: 0.75, CONTEXT: 0.75, LIVE: 1.5, LOW: 3.0, RERUN: 0.0, CATCHUP: 0.0}


def src_wait_s(job: "Job", now: float) -> float:
    """How long this job's audio request may still wait for the station's STT input now: its class's budget less the
    time since it was queued, and never more than half of what is left of its lease (it is still transcribed in time)."""
    left = SRC_WAIT_S.get(job.cls, 0.0) - max(0.0, now - (job.queued_at or now))
    return max(0.0, min(left, (job.expires - now) / 2.0))


def _budgets(job) -> tuple[float, float, float]:
    """(wait for a claim, first lease, lease cap) of this job: its class's, but a background clip placed on the stations
    (#896, station_only) waits its background budget for a claim and is leased like a context talkgroup's clip (a
    station's whisper decodes in seconds to tens of seconds, never a pre-alert lease)."""
    if job.extra.get("station_only"):
        return float(job.extra.get("claim_wait") or BUDGETS[LOW][0]), BUDGETS[LOW][1], BUDGETS[LOW][2]
    if job.norm:                                     # (Auto · normal: its start budget to be claimed, a LOW lease)
        return float(job.extra.get("claim_wait") or BUDGETS[LOW][0]), BUDGETS[LOW][1], BUDGETS[LOW][2]
    return BUDGETS[job.cls]


def rank(job) -> int:
    """Where a job queues (#896 + Auto · normal): priority work first (0), Auto · normal next (1), background and the
    catch-up last (2); then by class, then oldest first."""
    return 2 if job.bg else 1 if job.norm else 0


def priority(job) -> bool:
    """A priority-tier job (rank 0): what the pre-alert speed gate measures, what background and Auto · normal work
    yields to, and what never waits behind either. Never a shadow copy (the latency guard's probe) nor a clip placed on
    the stations (station_only: background or Auto · normal); a lower tier (a future challenger's) ranks above 0."""
    return rank(job) == 0 and not job.extra.get("shadow") and not job.extra.get("station_only")


def _extra(job) -> float:
    if job.extra.get("station_only") or job.norm:
        return PER_AUDIO_S * max(0.0, job.seconds)
    return 0.0 if job.cls in FAST_CLASSES or job.cls == CATCHUP else PER_AUDIO_S * max(0.0, job.seconds)
PENDING_MODEL = "gpu-worker"   # a row's model while a worker has it (the UI: "Transcribing (GPU)")


def model_label(model: str, worker: str = "", kind: str = "gpu") -> str:
    """The row's model for a worker's text: "large-v3-turbo-q5_0 (GPU gpu-1)", "small.en-q5_1 (station st-north)"."""
    name = (model or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].removeprefix("ggml-").removesuffix(".bin") or "whisper"
    if kind == "station":
        return f"{name} (station {worker.removeprefix('station:')})"
    return f"{name} (GPU {worker})" if worker else f"{name} (GPU)"


def _short(wid: str) -> str:
    return wid.removeprefix("station:")


def avoid_why(cand, avoid) -> str:
    """Why every candidate is aside: the hub's word on each ({worker: why}) or just "set aside"."""
    w = cand[0]
    why = avoid.get(w.id, "") if isinstance(avoid, dict) else ""
    return f"{_short(w.id)} {why or 'set aside'}" if len(cand) == 1 else f"{len(cand)} GPU workers set aside"


def class_key(cls: float) -> str:
    return CLASS_NAMES.get(cls, str(cls))


@dataclass(eq=False)
class Job:
    """A clip for a worker. audio(): the WAV a whisper-server takes (16 kHz mono 16-bit), None when it can't be read.
    on_done(job, result) / on_fallback(job, why): exactly one of them, once."""
    id: str
    cls: float
    channel: str
    kind: str                                   # "traffic" | "page"
    seconds: float
    audio: Callable[[], bytes | None]
    on_done: Callable[["Job", dict], None]
    on_fallback: Callable[["Job", str], None]
    prompt: str = ""
    clip_end: float | None = None
    queued_at: float = 0.0
    lease: str = ""
    worker: str = ""
    claimed_at: float | None = None
    expires: float = 0.0
    cap_at: float = 0.0
    fetched_at: float | None = None
    audio_wait: bool = False                    # audio(wait_s): it may wait that long for the station's STT input
    gpu_only: bool = False                      # a GPU worker's only (an AWS channel's clip: a station's whisper is
                                                # slower than AWS, so it never waits on one)
    forced: bool = False                        # its channel's engine sends it here (sizeup/sttroute.py): whatever the
                                                # class switches; its class's wait and lease still bound it
    pin: str = ""                               # that worker only (a pinned engine: "gpu-pc", "station:north")
    norm: bool = False                          # Auto · normal: after every priority job, before background work
    bg: bool = False                            # background work (Auto · background, the catch-up): after every other
                                                # job, and on a GPU only while no priority work is there (#878)
    avoid: frozenset = frozenset()              # workers it must not go to (the hub's latency guard)
    model: str = ""                             # the catalog model its channel uses (sizeup/sttmodels.py): a worker that
                                                # has it loaded first; any other decodes it on its own
    context: list = field(default_factory=list)   # a llama-asr model's context (sttmodels.context_list)
    plain: list = field(default_factory=list)   # the models its context is off for (whichever decodes it)
    extra: dict = field(default_factory=dict)   # the caller's own (catch-up, the page object, ...)

    def public(self) -> dict:
        """What the worker is told (never the audio itself: it fetches that with its lease)."""
        return {"lease": self.lease, "id": self.id, "kind": self.kind, "channel": self.channel,
                "class": class_key(self.cls), "prompt": self.prompt, "seconds": round(self.seconds, 2),
                "lease_s": round(max(0.0, self.expires - (self.claimed_at or 0.0)), 1), "cap_at": self.cap_at,
                "fast": self.cls in FAST_CLASSES or self.cls == CATCHUP or self.extra.get("kind") == "station",
                "audio": f"/api/station/stt/jobs/{self.lease}/audio",
                **({"model": self.model} if self.model else {}),
                **({"context": list(self.context)} if self.context else {}),
                **({"plain": list(self.plain)} if self.plain else {})}


@dataclass
class Worker:
    id: str
    kind: str = "gpu"                           # gpu | station (a station's own whisper, #639 phase 2)
    load: dict = field(default_factory=dict)    # a station's: its own queue, its CPU, its pace (what it says)
    state: str = "online"
    model: str = ""
    version: str = ""
    update: dict = field(default_factory=dict)  # a GPU worker's self-update: {sha256, commit, auto, state, bad}
    keep_awake: dict = field(default_factory=dict)  # a GPU worker's keep_awake on Windows: {on, held} (none: not said)
    bye: bool = False                           # its last hello / state said a clean stop (reason "shutdown")
    gpu: dict = field(default_factory=dict)
    seen: float = 0.0
    polling: int = 0
    fails: int = 0
    cool_until: float = 0.0
    cools: int = 0                              # cooldowns in a row (no job done since): each one twice as long
    fast: deque = field(default_factory=deque)  # (at, claim -> text s) of its priority pre-alert jobs, FAST_WINDOW_S; a
                                                # lease it lost counts as the whole cap (a gaming take-back doesn't
                                                # count, nor any Auto · normal or background job: priority() only)
    probe_at: float = 0.0                       # its last pre-alert probe while slow
    done: int = 0
    lost: int = 0
    last_job: float | None = None
    leases: set = field(default_factory=set)
    models: list = field(default_factory=list)  # a GPU worker's models loaded now: its whisper_server's, then the
                                                # catalog's that answer (its claim and heartbeat say)
    stt_state: dict = field(default_factory=dict)   # each model it was asked for: ready, downloading, no room ...


def pct(xs: list[float], p: float) -> float | None:
    """The p-th percentile (nearest rank) of xs, None when empty."""
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return round(s[k], 2)


class Pool:
    """The queue, the leases and the numbers. enabled(cls) -> bool: the pool is on for that class (Settings, and a
    worker registered at all). Thread-safe; fallbacks and results run outside the lock."""

    def __init__(self, enabled: Callable[[float], bool] = lambda cls: False, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, allowed: Callable[[str], bool] = lambda wid: True,
                 on: Callable[[], bool] = lambda: True):
        """allowed(worker id): its token still stands (a revoke racing a request never brings a worker back). on(): the
        pool's own switch (Settings), which a channel's engine choice never overrides (its class switches it does)."""
        self.enabled, self.clock, self.log, self.allowed, self.on = enabled, clock, log, allowed, on
        self._prio_at = 0.0                       # the last priority clip (not background) that came in
        self._cv = threading.Condition()
        self._queue: list[Job] = []
        self._leases: dict[str, Job] = {}
        self._open: set[str] = set()              # clip ids queued or leased
        self.workers: dict[str, Worker] = {}
        self._done: deque = deque()               # (at, class, channel, queued -> done s, claim -> done s, worker, kind)
        self.fallbacks = {r: 0 for r in REASONS}
        self.refused_late = 0
        self.submitted = 0
        self._fb_q: deque = deque()
        self._fb_cv = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- the threads ------------------------------------------------------------------------------------------------

    def start(self) -> Pool:
        if not self._threads:
            for name, fn in (("stt-pool", self._run), ("stt-pool-fallback", self._fallback_loop)):
                t = threading.Thread(target=fn, name=name, daemon=True)
                t.start()
                self._threads.append(t)
        return self

    def stop(self, fall_back: bool = True) -> int:
        """Stopped. fall_back: every job still waiting or leased falls back now; else they are left where their rows
        say they are (the hub stopping: "pending" on the AWS path, which the next start's recovery routes). How many."""
        self._stop.set()
        with self._cv:
            jobs = list(self._queue) + list(self._leases.values())
            self._queue.clear()
            self._leases.clear()
            self._open.clear()
            self._cv.notify_all()
        if fall_back:
            for j in jobs:
                self._call_fallback(j, "the hub is stopping")
        with self._fb_cv:
            self._fb_cv.notify_all()
        return len(jobs)

    def _run(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the pool's clock must never stop
                self.log(f"stt pool: tick: {type(e).__name__}: {e}")

    def _fallback_loop(self) -> None:
        while True:
            with self._fb_cv:
                while not self._fb_q and not self._stop.is_set():
                    self._fb_cv.wait(1.0)
                if not self._fb_q:
                    return
                job, why = self._fb_q.popleft()
            self._call_fallback(job, why)

    def _call_fallback(self, job: Job, why: str) -> None:
        try:
            job.on_fallback(job, why)
        except Exception as e:  # noqa: BLE001 - the caller's trouble: said, never the pool's end
            self.log(f"stt pool: {job.id}: fallback failed: {type(e).__name__}")

    def _fall(self, job: Job, reason: str, why: str) -> None:
        """Called with the lock held: out of the pool, its fallback queued (run on the fallback thread). The reason is
        on the job (extra["fell"]): a catch-up clip's lost lease is a failed try, nobody taking it is none."""
        self._open.discard(job.id)
        job.extra["fell"] = reason
        self.fallbacks[reason] = self.fallbacks.get(reason, 0) + 1
        with self._fb_cv:
            self._fb_q.append((job, why))
            self._fb_cv.notify()

    def drain(self) -> int:
        """Run every queued fallback now, on this thread (tests; the pool's own thread does it otherwise)."""
        n = 0
        while True:
            with self._fb_cv:
                if not self._fb_q:
                    return n
                job, why = self._fb_q.popleft()
            self._call_fallback(job, why)
            n += 1

    # -- workers ---------------------------------------------------------------------------------------------------

    def seen(self, wid: str, state: str | None = None, model: str | None = None, gpu: dict | None = None,
             version: str | None = None, kind: str | None = None, load: dict | None = None,
             update: dict | None = None, keep_awake: dict | None = None, reason: str | None = None,
             models: list | None = None, stt_state: dict | None = None) -> Worker:
        """A worker's request (hello, state, claim): its state and when it was last seen. A worker that is no longer
        online hands its pre-alert and Auto · normal jobs back at once (a normal clip has the stations and AWS still
        ahead of it; the rest run to their lease: it may finish them). None: a worker
        whose token no longer stands (revoked meanwhile)."""
        if wid not in self.workers and not self.allowed(wid):
            return None
        with self._cv:
            w = self.workers.get(wid)
            if w is None:
                w = self.workers[wid] = Worker(wid)
            w.seen = self.clock()
            if kind in KINDS:
                w.kind = kind
            if isinstance(load, dict):
                w.load = {k: load[k] for k in ("queue", "own", "cpu_pct", "service_s", "why", "max_s", "rtf")
                          if isinstance(load.get(k), (int, float, str)) and not isinstance(load.get(k), bool)}
            if state in STATES:
                if state != "online" and w.state == "online":
                    for lease in list(w.leases):
                        job = self._leases.get(lease)
                        if job is not None and (job.cls in FAST_CLASSES or job.norm):
                            self._end_lease(job)
                            self._fall(job, "released", f"GPU worker {wid} went {state}")
                w.state = state
            if model is not None:
                w.model = str(model)[:80]
            if version is not None:
                w.version = str(version)[:40]
            if isinstance(update, dict):
                w.update = dict(update)
            if isinstance(keep_awake, dict):
                w.keep_awake = dict(keep_awake)
            if reason is not None:
                w.bye = reason == "shutdown"
            if isinstance(models, list):
                w.models = [str(m)[:48] for m in models[:16] if isinstance(m, str)]
            if isinstance(stt_state, dict):
                w.stt_state = {str(k)[:48]: str(v)[:60] for k, v in list(stt_state.items())[:16]}
            if isinstance(gpu, dict):
                w.gpu = {k: gpu[k] for k in ("util_pct", "mem_used_mb", "mem_total_mb", "others", "name")
                         if isinstance(gpu.get(k), (int, float, str)) and not isinstance(gpu.get(k), bool)}
            self._cv.notify_all()
            return w

    def forget(self, wid: str, why: str = "revoked") -> int:
        """A worker revoked: what it holds falls back now, and it is gone from the list. How many jobs."""
        with self._cv:
            w = self.workers.pop(wid, None)
            jobs = [self._leases.pop(lease) for lease in list(w.leases if w else ()) if lease in self._leases]
            for j in jobs:
                self._fall(j, "error", f"GPU worker {wid} {why}")
            self._cv.notify_all()
        return len(jobs)

    def _ready(self, w: Worker, now: float) -> bool:
        return (w.state == "online" and (w.polling > 0 or now - w.seen < READY_S) and now >= w.cool_until)

    def holds_prealert(self, wid: str) -> bool:
        """This worker holds a pre-alert lease now: the LLM lane (sizeup/cloud/infer.py) gives it nothing meanwhile."""
        with self._cv:
            w = self.workers.get(wid)
            return w is not None and any(j is not None and j.cls in FAST_CLASSES
                                         for j in (self._leases.get(lease) for lease in w.leases))

    def ready_workers(self, now: float | None = None) -> list[str]:
        now = self.clock() if now is None else now
        with self._cv:
            return [w.id for w in self.workers.values() if self._ready(w, now)]

    def state_of(self, w: Worker, now: float) -> str:
        """online | gaming | paused | cooling | offline: what Health says of it."""
        if now - w.seen >= OFFLINE_S and w.polling <= 0:
            return "offline"
        if w.state == "online" and now < w.cool_until:
            return "cooling"
        return w.state

    def asleep(self, w: Worker, now: float) -> bool:
        """A GPU worker offline that looks asleep (Health's "(asleep?)"): its last word said keep_awake off or not held,
        or its heartbeats stopped while it was online (no pause, gaming or clean stop before). A guess, never an alert."""
        if w.kind != "gpu" or w.bye or self.state_of(w, now) != "offline":
            return False
        return w.state == "online" or w.keep_awake.get("on") is False or w.keep_awake.get("held") is False

    def _service_s(self, gpu: bool = False) -> float:
        """The median claim -> text of the last 50 jobs (gpu: the GPU workers' alone, never a station's whisper: by the
        kind it had when it was done, so a worker gone since never passes for a GPU)."""
        took = [x[4] for x in self._done if x[4] is not None and (not gpu or x[6] == "gpu")][-50:]
        return pct(took, 50) or SERVICE_DEFAULT_S

    def _fast_s(self, w: Worker, now: float) -> float | None:
        """This worker's median claim -> text on pre-alert jobs over FAST_WINDOW_S (a lost lease: its cap); None: none."""
        while w.fast and now - w.fast[0][0] > FAST_WINDOW_S:
            w.fast.popleft()
        return pct([s for _at, s in w.fast], 50)

    def _fast_ok(self, w: Worker, now: float) -> bool:
        """May this worker have a pre-alert clip now: idle (in a claim, holding nothing), and quick on them lately, or
        slow but due a probe (PROBE_S since its last slow one or probe)."""
        if w.polling <= 0 or w.leases or not self._ready(w, now):
            return False
        s = self._fast_s(w, now)
        if s is None or s <= PREALERT_MAX_SERVICE_S:
            return True
        last = max(w.probe_at, w.fast[-1][0] if w.fast else 0.0)
        return now - last >= PROBE_S

    def word(self, sid: str, now: float | None = None, avoid=frozenset()) -> dict:
        """What the pool tells a station (its state answer, #639 phase 2): ready, the workers other than its own whisper
        that could take a context clip of its now; max_s, the longest clip one of them would take; gpu_idle(_n), the GPU
        workers idle and quick on pre-alert clips now (a stopping station hands over one pre-alert clip each at most);
        gpu_ready, the GPU workers ready for an Auto · priority clip (none in avoid: the hub's guard); gpu_bg, one would
        take a background clip now (_bg_free)."""
        now = self.clock() if now is None else now
        on = self._on()                           # (a setting read: never under the pool's lock)
        with self._cv:
            others = [w for w in self.workers.values() if self._ready(w, now) and w.id != f"station:{sid}"]
            max_s = max((float(w.load.get("max_s", 1e9)) if w.kind == "station" else 1e9 for w in others), default=0.0)
            gpu_idle_n = sum(1 for w in others if w.kind == "gpu" and self._fast_ok(w, now))
            # the engine selector's (sizeup/sttroute.py): GPU workers an Auto · priority clip could go to (the guard's
            # aside), and whether one would take a background clip now
            gpu_ready = sum(1 for w in others if w.kind == "gpu" and w.id not in avoid) if on else 0
            gpu_bg = on and any(w.kind == "gpu" and w.id not in avoid and self._bg_free(w, now) for w in others)
        return {"ready": len(others), "max_s": round(min(max_s, 3600.0), 1), "gpu_idle": gpu_idle_n > 0,
                "gpu_idle_n": gpu_idle_n, "gpu_ready": gpu_ready, "gpu_bg": bool(gpu_bg)}

    def _gpu_idle(self, now: float) -> bool:
        return any(w.kind == "gpu" and w.polling > 0 and not w.leases and self._ready(w, now)
                   for w in self.workers.values())

    def _on(self) -> bool:
        try:
            return bool(self.on())
        except Exception:  # noqa: BLE001 - a setting that can't be read: off
            return False

    def _station_could(self, j: Job, now: float) -> bool:
        """(lock held) A station's whisper in the pool could take this background job now (not its own station's)."""
        return any(w.kind == "station" and self._ready(w, now) and w.polling > 0
                   and w.id != f"station:{j.extra.get('origin') or ''}" and j.seconds <= float(w.load.get("max_s", 1e9))
                   for w in self.workers.values())

    def _holds_bg(self, w: Worker) -> bool:
        """(lock held) This worker holds background or Auto · normal work now (it decodes one clip at a time): busy for a
        priority clip (it goes on, AWS), never one queued behind it."""
        return any(rank(self._leases[lease]) > 0 for lease in w.leases if lease in self._leases)

    def _bg_free(self, w: Worker, now: float, seconds: float | None = None) -> bool:
        """(lock held) May this GPU worker take background work now (#878): it holds nothing and is in a claim, no
        other job waits, no priority clip came in the last BG_QUIET_S, and the clip is short (it is back at once)."""
        return (self._ready(w, now) and w.polling > 0 and not w.leases and now - self._prio_at >= BG_QUIET_S
                and not any(not j.bg and not j.extra.get("station_only") for j in self._queue)
                and (seconds is None or seconds <= BG_GPU_MAX_S))

    def _norm_free(self, w: Worker, now: float) -> bool:
        """(lock held) May this GPU worker start Auto · normal work now: it holds nothing and is in a claim, no priority
        job it could take waits (it goes first), and no priority clip came in the last NORM_QUIET_S (a page right behind a normal
        decode would wait for it: a worker decodes one clip at a time)."""
        return (self._ready(w, now) and w.polling > 0 and not w.leases and now - self._prio_at >= NORM_QUIET_S
                and not any(priority(j) and self._fits(w, j, now) for j in self._queue))

    def _fits(self, w: Worker, j: Job, now: float) -> bool:
        """May this worker take this job now. A station's whisper: never a pre-alert clip nor an AWS channel's (slower
        than AWS), and only once a GPU worker that could take it now has had STATION_DEFER_S to. A GPU worker: a
        pre-alert clip only when _fast_ok (a forced one: when it holds nothing), background work only when _bg_free.
        A pinned job: its worker only; never a worker in its avoid."""
        if (j.pin and w.id != j.pin) or w.id in j.avoid or (j.extra.get("station_only") and w.kind != "station"):
            return False
        if j.bg and w.kind == "gpu":             # (#896: stations first: a GPU only once no station could take it)
            return self._bg_free(w, now, j.seconds) and (bool(j.pin) or bool(j.extra.get("overflow"))
                                                         or now - j.queued_at >= BG_STATION_FIRST_S
                                                         or not self._station_could(j, now))
        if w.kind == "station":
            if (j.cls in FAST_CLASSES and j.pin != w.id and not j.bg and not j.extra.get("station_only")) or j.gpu_only or \
                    j.extra.get("origin") == w.id.removeprefix("station:"):
                return False                       # (never a station's own clip back to it: no round trip)
            if j.seconds > float(w.load.get("max_s", 1e9)):
                return False                       # (only what it decodes in a few seconds: its own pages never wait long)
            return (not (now - j.queued_at < STATION_DEFER_S and self._gpu_idle(now)) or j.pin == w.id or j.bg
                    or bool(j.extra.get("station_only")))
        if j.model and j.model not in w.models and now - j.queued_at < MODEL_DEFER_S and self._model_idle(j, w, now):
            return False                           # (a worker with its model takes it in a moment: never a wait)
        if rank(j) == 0 and self._holds_bg(w):
            return False                           # (#878: never queued behind its background or normal decode)
        if j.norm:                                 # (Auto · normal: after the priority work, leased like LOW: a free GPU
            return self._norm_free(w, now)         # with no priority work waiting or just in)
        if j.cls in FAST_CLASSES and j.forced:     # (the pre-alert speed gate too; AWS out: any free worker)
            return w.polling > 0 and not w.leases and self._ready(w, now) and (
                bool(j.extra.get("queue_ok")) or self._fast_ok(w, now))
        return j.cls not in FAST_CLASSES or self._fast_ok(w, now)

    def _model_idle(self, j: Job, w: Worker, now: float) -> bool:
        """Another GPU worker has this job's model loaded and could take the job now (in a claim, room for a lease;
        for a pre-alert clip: quick and holding nothing)."""
        return any(o is not w and o.kind == "gpu" and j.model in o.models and o.polling > 0 and self._ready(o, now)
                   and o.id not in j.avoid and (not j.pin or o.id == j.pin) and not (priority(j) and self._holds_bg(o))
                   and len(o.leases) < MAX_LEASES and (j.cls not in FAST_CLASSES or self._fast_ok(o, now))
                   for o in self.workers.values())

    def ready(self, cls: float, now: float | None = None, gpu_only: bool = False, origin: str = "",
              seconds: float | None = None) -> tuple[bool, str]:
        """Can the pool take a clip of this class now: (yes, why not). gpu_only: on a GPU worker (an AWS channel's clip:
        a station's whisper is slower than AWS); a pre-alert class always is."""
        now = self.clock() if now is None else now
        if cls not in BUDGETS:
            return False, "not a pool class"
        try:
            if not self.enabled(cls):
                return False, "off"
        except Exception:  # noqa: BLE001 - a setting that can't be read: off
            return False, "off"
        if self._stop.is_set():
            return False, "stopped"
        with self._cv:
            ready = [w for w in self.workers.values() if self._ready(w, now)
                     and (w.kind == "gpu" or not (gpu_only or cls in FAST_CLASSES
                                                  or (origin and w.id == f"station:{origin}")
                                                  or (seconds is not None and seconds > float(w.load.get("max_s", 1e9)))))]
            if not ready:
                return False, "no worker ready"
            if cls in FAST_CLASSES:
                # a pre-alert clip: only to a worker idle now (in a claim: never one busy on a catch-up clip), and only
                # while the workers are quick on these (else AWS, which answers them in seconds)
                idle = [w for w in ready if w.polling > 0 and not w.leases]
                if not idle:
                    return False, "no worker idle now"
                if any(j.cls in FAST_CLASSES and priority(j) for j in self._queue):
                    return False, "a pre-alert clip waits already"
                if not any(self._fast_ok(w, now) for w in idle):
                    svc = min(self._fast_s(w, now) or 0.0 for w in idle)
                    return False, f"slow on pre-alert clips lately ({svc:.1f} s)"
                return True, ""
            if any(w.polling > 0 and len(w.leases) < MAX_LEASES for w in ready) and \
                    not any(j.cls <= cls and priority(j) for j in self._queue):
                return True, ""                    # a worker in a claim now and nothing ahead: it takes it at once
            # (the priority work ahead of it: Auto · normal and background work never is, it yields)
            ahead = sum(1 for j in self._queue if j.cls <= cls and priority(j)) + sum(
                1 for j in self._leases.values() if priority(j))
            est = ahead * self._service_s() / len(ready)
        if est > BUDGETS[cls][0]:
            return False, f"busy (about {est:.0f} s of work ahead)"
        return True, ""

    def ready_forced(self, cls: float, now: float | None = None, pin: str = "", avoid=frozenset(), bg: bool = False,
                     seconds: float | None = None, origin: str = "", queue_ok: bool = False) -> tuple[bool, str]:
        """Can a clip a channel's engine sends here (sizeup/sttroute.py) go to the pool now: (yes, why not, said as the
        route says it: "gpu-pc gaming", "gpu-pc busy (background)"). Whatever the class switches (the pool's own switch
        still holds) and whatever the worker's quick record (the hub's guard has its say through avoid). pin: that
        worker only. bg: background (_bg_free on a GPU; a station's whisper as for any context clip). A pre-alert
        class only to a worker that holds nothing, and not behind another waiting: never a wait behind other work
        (queue_ok: AWS can't take it either: it may wait its class's claim time for a busy worker rather than go back
        to the station)."""
        now = self.clock() if now is None else now
        if cls not in BUDGETS:
            return False, "not a pool class"
        if not self._on():
            return False, "pool off"
        if self._stop.is_set():
            return False, "stopped"
        with self._cv:
            cand = [w for w in self.workers.values() if (w.id == pin if pin else w.kind == "gpu" or bg)]
            if not cand:
                return False, f"{pin} not connected" if pin else "no GPU worker online"
            fine = [w for w in cand if w.id not in avoid]
            if not fine:
                return False, avoid_why(cand, avoid)
            ready = [w for w in fine if self._ready(w, now)]
            if not ready:
                w = fine[0]
                return False, f"{_short(w.id)} {self.state_of(w, now)}" if len(fine) == 1 else "no GPU worker ready"
            if bg:
                gpu = [w for w in ready if w.kind == "gpu"]
                if any(self._bg_free(w, now, seconds) for w in gpu):
                    return True, ""
                st = [w for w in ready if w.kind == "station"        # (background: any class, #896)
                      and w.id != f"station:{origin}" and (seconds is None or seconds <= float(w.load.get("max_s", 1e9)))]
                if st:
                    return True, ""
                return False, "busy with priority work" if gpu else "no worker free"
            idle = [w for w in ready if w.polling > 0 and not w.leases]

            def busy_why(w: Worker) -> str:
                held = [self._leases.get(lease) for lease in w.leases]
                tier = (" (background)" if held and all(j is not None and j.bg for j in held) else
                        " (normal)" if held and all(j is not None and rank(j) > 0 for j in held) else "")
                return f"{_short(w.id)} busy{tier}"
            if cls in FAST_CLASSES:
                if any(j.cls in FAST_CLASSES and priority(j) for j in self._queue):
                    return False, "a pre-alert clip waits already"
                if queue_ok:                      # (AWS out: it may wait for a GPU busy with priority work, never for
                    waitable = [w for w in ready if not self._holds_bg(w)]    # one decoding normal or background work)
                    return (True, "") if waitable else (False, busy_why(ready[0]))
                if not idle:
                    return False, busy_why(ready[0])
                # the pool's pre-alert speed gate holds for a channel's engine too (_fast_ok: quick lately, or due a probe)
                if not any(self._fast_ok(w, now) for w in idle):
                    svc = min(self._fast_s(w, now) or 0.0 for w in idle)
                    return False, f"{_short(idle[0].id)} slow on pre-alert clips lately ({svc:.1f} s)"
                return True, ""
            # a worker decodes one clip at a time: one holding background work is busy for a priority clip (#878: never
            # queued behind it; AWS instead)
            free = [w for w in ready if not self._holds_bg(w)]
            if not free:
                return False, busy_why(ready[0])
            if any(w in idle for w in free) and not any(priority(j) and j.cls <= cls for j in self._queue):
                return True, ""
            ahead = sum(1 for j in self._queue if priority(j) and j.cls <= cls) + sum(
                1 for w in free for _lease in w.leases)
            est = ahead * self._service_s() / len(free)
        if est > BUDGETS[cls][0]:
            return False, f"busy (about {est:.0f} s of work ahead)"
        return True, ""

    def station_wait(self, w: Worker) -> float:
        """(lock held) A station's whisper's estimated wait for a new clip: its queue (and what the pool leased it) x its
        measured time per clip (what it reports in its load)."""
        svc = float(w.load.get("service_s") or SERVICE_DEFAULT_S * 8)
        own = w.load.get("own", w.load.get("queue"))       # (its own clips: never the catch-up's, which yield)
        return (float(own or 0) + len(w.leases)) * svc

    def bg_place(self, cls: float, seconds: float, origin: str = "", budget: float = 90.0,
                 avoid=frozenset(), now: float | None = None, stations: bool = True,
                 gpu: bool = True) -> tuple[str, str, float]:
        """Where an Auto · background clip its station couldn't start in time goes (#896): ("station", the soonest
        station's worker id, its estimated wait) when a station whisper in the pool can START it within budget (never
        its own station: it was behind; the job then goes to whichever station claims it first: station_only); else
        ("gpu", worker id, 0) for an idle GPU with no priority work (overflow); else ("", "", the best station's wait or
        -1 with none). A station's wait counts its queue, its leases and its share of the station-only clips waiting."""
        now = self.clock() if now is None else now
        if not self._on() or self._stop.is_set():
            return "", "", -1.0
        with self._cv:
            cands = [w for w in self.workers.values() if stations and w.kind == "station" and self._ready(w, now)
                     and w.id != f"station:{origin}" and seconds <= float(w.load.get("max_s", 1e9))]
            ahead = sum(1 for j in self._queue if j.extra.get("station_only"))
            best = None
            for w in cands:
                svc = float(w.load.get("service_s") or SERVICE_DEFAULT_S * 8)
                est = self.station_wait(w) + ahead / len(cands) * svc
                if best is None or est < best[1]:
                    best = (w.id, est)
            if best is not None and best[1] <= budget:
                return "station", best[0], round(best[1], 1)
            free = next((w.id for w in self.workers.values() if gpu and w.kind == "gpu" and w.id not in avoid
                         and self._bg_free(w, now, seconds)), "")
            if free:
                return "gpu", free, 0.0
        return "", "", round(best[1], 1) if best is not None else -1.0

    def norm_place(self, seconds: float, avoid=frozenset(), budget: float = 25.0,
                   now: float | None = None) -> tuple[bool, str, float]:
        """Can an Auto · normal clip START on a GPU within budget: (yes, why not, the estimated wait). The work ahead of
        it there: every priority job waiting and the normal ones already waiting (it queues after them: FIFO), and what
        the GPUs hold now, x the measured time per clip, over the GPU workers ready (none gaming, paused, cooling, gone
        or kept off by the guard: avoid)."""
        now = self.clock() if now is None else now
        if not self._on() or self._stop.is_set():
            return False, "pool off", -1.0
        with self._cv:
            gpus = [w for w in self.workers.values() if w.kind == "gpu"]
            if not gpus:
                return False, "no GPU worker online", -1.0
            ready = [w for w in gpus if w.id not in avoid and self._ready(w, now)]
            if not ready:
                w = gpus[0]
                why = (avoid_why(gpus, avoid) if all(x.id in avoid for x in gpus)
                       else f"{_short(w.id)} {self.state_of(w, now)}" if len(gpus) == 1 else "no GPU worker ready")
                return False, why, -1.0
            if seconds > BG_GPU_MAX_S:
                return False, f"too long for the GPU after priority work ({seconds:.0f} s)", -1.0
            ids = {w.id for w in ready}
            ahead = sum(1 for j in self._queue if rank(j) <= 1 and not j.extra.get("station_only")
                        and (not j.pin or j.pin in ids) and not ids <= set(j.avoid))
            ahead += sum(len(w.leases) for w in ready)
            est = ahead * self._service_s(gpu=True) / len(ready)
        if est > budget:
            return False, f"GPU busy (about {est:.0f} s ahead)", round(est, 1)
        return True, "", round(est, 1)

    def station_waits(self, now: float | None = None) -> dict:
        """Health: each pooled station's estimated wait for a new clip (s)."""
        now = self.clock() if now is None else now
        with self._cv:
            return {w.id.removeprefix("station:"): round(self.station_wait(w), 1) for w in self.workers.values()
                    if w.kind == "station" and self.state_of(w, now) != "offline"}

    # -- jobs ------------------------------------------------------------------------------------------------------

    def has(self, cid: str) -> bool:
        with self._cv:
            return cid in self._open

    def submit(self, job: Job) -> bool:
        """Queued (True), or not taken: the pool not ready for its class, or the clip already open here. The caller
        goes its own way on False; on True exactly one of on_done / on_fallback follows."""
        if job.norm and job.forced:               # (Auto · normal: placed already, norm_place / bg_place)
            ok = self._on()
        elif job.forced or job.pin:
            ok, _why = self.ready_forced(job.cls, pin=job.pin, avoid=job.avoid, bg=job.bg, seconds=job.seconds,
                                         origin=str(job.extra.get("origin") or ""),
                                         queue_ok=bool(job.extra.get("queue_ok")))
        else:
            ok, _why = self.ready(job.cls, gpu_only=job.gpu_only, origin=str(job.extra.get("origin") or ""),
                                  seconds=job.seconds)
        if not ok or self._stop.is_set():
            return False
        with self._cv:
            if job.id in self._open:
                return False
            job.queued_at = self.clock()
            if priority(job):
                self._prio_at = job.queued_at      # (background work keeps off a GPU for BG_QUIET_S after it)
            self._queue.append(job)
            self._open.add(job.id)
            self.submitted += 1
            self._cv.notify_all()
        return True

    def _best(self, w: Worker | None = None, now: float | None = None) -> Job | None:
        """The next job for this worker, among those it may take (_fits): by tier (rank), then a priority or background
        job by its class, oldest queued first; Auto · normal by its air time (clip_end: a clip handed over late never
        jumps ahead of one said before it), whatever its class."""
        now = self.clock() if now is None else now

        def key(j: Job):
            if rank(j) == 1:
                return (1, 0, j.clip_end if j.clip_end is not None else j.queued_at, j.queued_at)
            return (rank(j), j.cls, j.queued_at, 0.0)
        return min((j for j in self._queue if w is None or self._fits(w, j, now)), key=key, default=None)

    def claim(self, wid: str, wait: float = 0.0, state: str | None = None, model: str | None = None,
              kind: str | None = None, load: dict | None = None, models: list | None = None) -> Job | None:
        """A worker's long poll: the best job (leased to it) or None after `wait`. Only an online worker gets one.
        models: the models it has loaded now (a job for one of them goes to it first: _fits)."""
        wait = max(0.0, min(CLAIM_WAIT_MAX_S, float(wait)))
        end = time.monotonic() + wait
        w = self.seen(wid, state, model, kind=kind, load=load, models=models)
        if w is None:
            return None
        with self._cv:
            w.polling += 1
            try:
                while not self._stop.is_set():
                    now = self.clock()
                    w.seen = now
                    if wid not in self.workers:          # revoked meanwhile
                        return None
                    if self._ready(w, now) and len(w.leases) < MAX_LEASES:
                        job = self._best(w, now)
                        if job is not None:
                            self._queue.remove(job)
                            if job.cls in FAST_CLASSES and priority(job) and \
                                    (self._fast_s(w, now) or 0.0) > PREALERT_MAX_SERVICE_S:
                                w.probe_at = now               # a slow worker's probe: the next one PROBE_S on
                            return self._lease(job, w, now)
                    left = end - time.monotonic()
                    if left <= 0:
                        return None
                    # (a job held back for a worker with its model: looked at again once its defer is over)
                    defer = [j.queued_at + MODEL_DEFER_S - now for j in self._queue if j.model
                             and now - j.queued_at < MODEL_DEFER_S]
                    self._cv.wait(min(left, 1.0, *(max(0.02, d) for d in defer)))
                return None
            finally:
                w.polling -= 1

    def _lease(self, job: Job, w: Worker, now: float) -> Job:
        _wait, lease_s, cap_s = _budgets(job)
        extra = _extra(job)
        job.lease, job.worker, job.claimed_at = secrets.token_hex(8), w.id, now
        job.extra["kind"] = w.kind
        job.expires, job.cap_at = now + lease_s + extra, now + cap_s + extra
        self._leases[job.lease] = job
        w.leases.add(job.lease)
        w.last_job = now
        return job

    def _held(self, wid: str, lease: str) -> Job | None:
        job = self._leases.get(lease)
        return job if job is not None and job.worker == wid and self.clock() < job.expires else None

    def job_for(self, wid: str, lease: str) -> Job | None:
        """The job of a live lease this worker holds (its audio route), else None."""
        with self._cv:
            job = self._held(wid, lease)
            if job is not None and job.fetched_at is None:
                job.fetched_at = self.clock()
            return job

    def extend(self, wid: str, lease: str) -> Job | None:
        """The lease renewed (its first length again, never past its cap): the job, or None (lost)."""
        with self._cv:
            job = self._held(wid, lease)
            if job is None:
                return None
            now = self.clock()
            _wait, lease_s, _cap = _budgets(job)
            job.expires = min(job.cap_at, now + lease_s + _extra(job))
            return job

    def _end_lease(self, job: Job, took: float | None = None, lost: bool = False) -> None:
        """The lease over. A pre-alert job's claim -> end goes to its worker's quick gate: took (its text came), or its
        whole cap when the lease was lost (ran out, the worker unheard); a job handed back or taken back on gaming or a
        pause isn't slowness and isn't counted."""
        w = self.workers.get(job.worker)
        # (only a priority job's: an Auto · normal or background clip of a pre-alert class, the latency guard's shadow
        # copy, is decoded under its own longer lease and says nothing of how quick the worker is on pre-alert clips)
        if w is not None and job.cls in FAST_CLASSES and priority(job) and job.lease in self._leases and (
                took is not None or lost):
            w.fast.append((self.clock(), took if took is not None else BUDGETS[job.cls][2]))
        self._leases.pop(job.lease, None)
        if w is not None:
            w.leases.discard(job.lease)

    def _failed(self, wid: str, now: float) -> None:
        w = self.workers.get(wid)
        if w is not None:
            w.fails += 1
            w.lost += 1
            if w.fails >= FAIL_N:
                cool = min(COOLDOWN_MAX_S, COOLDOWN_S * 2 ** w.cools)
                w.cool_until, w.fails, w.cools = now + cool, 0, w.cools + 1
                self.log(f"stt pool: GPU worker {wid}: {FAIL_N} jobs lost in a row: no new ones for {cool:.0f} s")

    def complete(self, wid: str, lease: str, result: dict) -> bool:
        """The worker's text for a live lease it holds: on_done (True). A lease gone (expired, handed on): refused,
        counted, nothing applied (False)."""
        with self._cv:
            job = self._held(wid, lease)
            if job is None:
                self.refused_late += 1
                return False
            now = self.clock()
            self._end_lease(job, now - job.claimed_at if job.claimed_at is not None else None)
            self._open.discard(job.id)
            w = self.workers.get(wid)
            if w is not None:
                w.fails, w.cools, w.done = 0, 0, w.done + 1
            self._done.append((now, job.cls, job.channel, now - job.queued_at,
                               now - job.claimed_at if job.claimed_at is not None else None, wid,
                               w.kind if w is not None else str(job.extra.get("kind") or "gpu")))
            while self._done and now - self._done[0][0] > WINDOW_S:
                self._done.popleft()
            self._cv.notify_all()
        try:
            job.on_done(job, result)
        except Exception as e:  # noqa: BLE001 - its text couldn't be applied: the next engine takes it (never dropped)
            self.log(f"stt pool: {job.id}: the result couldn't be applied ({type(e).__name__}): falling back")
            with self._cv:
                self.fallbacks["error"] += 1
            job.extra["fell"] = "error"
            self._call_fallback(job, f"the GPU worker's text couldn't be applied: {type(e).__name__}")
        return True

    def release(self, wid: str, lease: str, why: str = "") -> bool:
        """The worker hands the job back (gaming, its whisper-server failing, shutting down): the next engine now."""
        with self._cv:
            job = self._held(wid, lease)
            if job is None:
                return False
            self._end_lease(job)
            self._failed(wid, self.clock())
            self._fall(job, "released", f"GPU worker {wid} handed it back" + (f": {why[:120]}" if why else ""))
            self._cv.notify_all()
        return True

    def discard(self, job: Job, why: str) -> None:
        """A job whose text came but is no transcript (a repetition loop, its context read back): counted, and its
        fallback queued (the next engine), on the fallback thread: from on_done, never a second fallback."""
        with self._cv:
            job.extra["fell"] = "discarded"
            self.fallbacks["discarded"] = self.fallbacks.get("discarded", 0) + 1
        with self._fb_cv:
            self._fb_q.append((job, why))
            self._fb_cv.notify()

    def abandon(self, lease: str, why: str) -> bool:
        """A leased job the hub itself can't serve (its audio can't be read): the next engine now, the worker not
        blamed."""
        with self._cv:
            job = self._leases.get(lease)
            if job is None:
                return False
            self._end_lease(job)
            self._fall(job, "error", why)
            self._cv.notify_all()
        return True

    def tick(self, now: float | None = None) -> int:
        """Waits and leases run out: those jobs fall back. With no worker ready at all, every waiting job falls back
        at once (nobody could take it); with no GPU worker ready, every GPU-only one does (an Auto · normal clip: the
        stations, then AWS, never its whole 25 s wait for a GPU gone). How many fell back."""
        now = self.clock() if now is None else now
        n = 0
        with self._cv:
            for lease, job in list(self._leases.items()):
                w = self.workers.get(job.worker)
                if now < job.expires and w is not None and now - w.seen >= LOST_S:
                    self._end_lease(job, lost=True)    # its worker unheard: gone (a crash, the network), not slow
                    self._failed(job.worker, now)
                    self._fall(job, "lease", f"GPU worker {job.worker}: no word for {now - w.seen:.0f} s")
                    n += 1
                elif now >= job.expires:
                    self._end_lease(job, lost=True)
                    self._failed(job.worker, now)
                    past_cap = now >= job.cap_at
                    self._fall(job, "lease", f"GPU worker {job.worker}: no text in time"
                               + (" (its lease cap)" if past_cap else " (its lease ran out)"))
                    n += 1
            anyone = any(self._ready(w, now) for w in self.workers.values())
            gpu = any(w.kind == "gpu" and self._ready(w, now) for w in self.workers.values())
            for job in list(self._queue):
                if not anyone or (job.gpu_only and not gpu):     # (a GPU-only job: a pooled station never takes it)
                    self._queue.remove(job)
                    self._fall(job, "no_worker", "no pool worker ready" if not anyone else "no GPU worker ready")
                    n += 1
                elif now - job.queued_at >= _budgets(job)[0]:
                    self._queue.remove(job)
                    self._fall(job, "unclaimed", f"no pool worker took it in {_budgets(job)[0]:g} s")
                    n += 1
            if n:
                self._cv.notify_all()
        return n

    # -- the numbers -----------------------------------------------------------------------------------------------

    def stats(self) -> dict:
        """Settings > Station > Health: the workers, the queue by class, done and fallbacks, queued -> done p50/p90."""
        now = self.clock()
        with self._cv:
            done = [x for x in self._done if now - x[0] <= WINDOW_S]
            hour = [x for x in done if now - x[0] <= 3600.0]
            workers = []
            for w in sorted(self.workers.values(), key=lambda x: x.id):
                mine = [x for x in hour if x[5] == w.id]
                workers.append({"id": w.id, "kind": w.kind, "load": dict(w.load),
                                "state": self.state_of(w, now), "model": w.model, "version": w.version,
                                "update": dict(w.update), "keep_awake": dict(w.keep_awake),
                                "asleep": self.asleep(w, now), "models": list(w.models), "stt_state": dict(w.stt_state),
                                "gpu": dict(w.gpu), "seen": w.seen, "seen_s": round(now - w.seen, 1),
                                "jobs_h": len(mine), "done": w.done, "lost": w.lost, "leases": len(w.leases),
                                "cool_until": w.cool_until if w.cool_until > now else None})
            queued: dict[str, int] = {}
            for j in self._queue:
                queued[class_key(j.cls)] = queued.get(class_key(j.cls), 0) + 1
            total = [x[3] for x in done]
            by_class: dict[str, list[float]] = {}
            for x in done:
                by_class.setdefault(class_key(x[1]), []).append(x[3])
            out = {"workers": workers, "queued": len(self._queue), "queued_by_class": queued,
                   "leased": len(self._leases), "submitted": self.submitted, "done_h": len(hour),
                   "done_2h": len(done), "fallbacks": dict(self.fallbacks), "refused_late": self.refused_late,
                   "p50_s": pct(total, 50), "p90_s": pct(total, 90),
                   "classes": {k: {"n": len(v), "p50_s": pct(v, 50), "p90_s": pct(v, 90)} for k, v in by_class.items()},
                   "ready": sum(1 for w in self.workers.values() if self._ready(w, now)),
                   "service_s": round(self._service_s(), 2)}
        return out
