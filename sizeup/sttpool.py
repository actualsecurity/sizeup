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
REASONS = ("unclaimed", "lease", "released", "no_worker", "error")   # why a job fell back
CLASSES = tuple(BUDGETS)
# the pre-alert classes: an absolute deadline (no per-audio extension), only an idle quick worker, one greedy pass
# (whisper-server's temperature_inc 0, as the station's WhisperClient fast=True), so a hard clip is never decoded five
# more times past its lease, and back at once when their worker starts gaming or pauses
FAST_CLASSES = (URGENT, HIGH, CONTEXT_FALLBACK, CONTEXT)


def _extra(job) -> float:
    return 0.0 if job.cls in FAST_CLASSES or job.cls == CATCHUP else PER_AUDIO_S * max(0.0, job.seconds)
PENDING_MODEL = "gpu-worker"   # a row's model while a worker has it (the UI: "Transcribing (GPU)")


def model_label(model: str, worker: str = "", kind: str = "gpu") -> str:
    """The row's model for a worker's text: "large-v3-turbo-q5_0 (GPU gpu-1)", "small.en-q5_1 (station st-north)"."""
    name = (model or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].removeprefix("ggml-").removesuffix(".bin") or "whisper"
    if kind == "station":
        return f"{name} (station {worker.removeprefix('station:')})"
    return f"{name} (GPU {worker})" if worker else f"{name} (GPU)"


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
    gpu: dict = field(default_factory=dict)
    seen: float = 0.0
    polling: int = 0
    fails: int = 0
    cool_until: float = 0.0
    cools: int = 0                              # cooldowns in a row (no job done since): each one twice as long
    fast: deque = field(default_factory=deque)  # (at, claim -> text s) of its pre-alert jobs, FAST_WINDOW_S; a lease
                                                # it lost counts as the whole cap (a gaming take-back doesn't count)
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
                 log: Callable[[str], None] = print, allowed: Callable[[str], bool] = lambda wid: True):
        """allowed(worker id): its token still stands (a revoke racing a request never brings a worker back)."""
        self.enabled, self.clock, self.log, self.allowed = enabled, clock, log, allowed
        self._cv = threading.Condition()
        self._queue: list[Job] = []
        self._leases: dict[str, Job] = {}
        self._open: set[str] = set()              # clip ids queued or leased
        self.workers: dict[str, Worker] = {}
        self._done: deque = deque()               # (at, class, channel, queued -> done s, claim -> done s, worker)
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
            self.log(f"stt pool: {job.id}: fallback failed: {type(e).__name__}: {e}")

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
             update: dict | None = None, models: list | None = None, stt_state: dict | None = None) -> Worker:
        """A worker's request (hello, state, claim): its state and when it was last seen. A worker that is no longer
        online hands its pre-alert jobs back at once (the rest run to their lease: it may finish them). None: a worker
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
                        if job is not None and job.cls in FAST_CLASSES:
                            self._end_lease(job)
                            self._fall(job, "released", f"GPU worker {wid} went {state}")
                w.state = state
            if model is not None:
                w.model = str(model)[:80]
            if version is not None:
                w.version = str(version)[:40]
            if isinstance(update, dict):
                w.update = dict(update)
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

    def _service_s(self) -> float:
        """The median claim -> text of the last 50 jobs."""
        took = [x[4] for x in self._done if x[4] is not None][-50:]
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

    def word(self, sid: str, now: float | None = None) -> dict:
        """What the pool tells a station (its state answer, #639 phase 2): ready, the workers other than its own whisper
        that could take a context clip of its now; max_s, the longest clip one of them would take; gpu_idle(_n), the GPU
        workers idle and quick on pre-alert clips now (a stopping station hands over one pre-alert clip each at most)."""
        now = self.clock() if now is None else now
        with self._cv:
            others = [w for w in self.workers.values() if self._ready(w, now) and w.id != f"station:{sid}"]
            max_s = max((float(w.load.get("max_s", 1e9)) if w.kind == "station" else 1e9 for w in others), default=0.0)
            gpu_idle_n = sum(1 for w in others if w.kind == "gpu" and self._fast_ok(w, now))
        return {"ready": len(others), "max_s": round(min(max_s, 3600.0), 1), "gpu_idle": gpu_idle_n > 0,
                "gpu_idle_n": gpu_idle_n}

    def _gpu_idle(self, now: float) -> bool:
        return any(w.kind == "gpu" and w.polling > 0 and not w.leases and self._ready(w, now)
                   for w in self.workers.values())

    def _fits(self, w: Worker, j: Job, now: float) -> bool:
        """May this worker take this job now. A station's whisper: never a pre-alert clip nor an AWS channel's (slower
        than AWS), and only once a GPU worker that could take it now has had STATION_DEFER_S to. A GPU worker: a
        pre-alert clip only when _fast_ok."""
        if w.kind == "station":
            if j.cls in FAST_CLASSES or j.gpu_only or j.extra.get("origin") == w.id.removeprefix("station:"):
                return False                       # (never a station's own clip back to it: no round trip)
            if j.seconds > float(w.load.get("max_s", 1e9)):
                return False                       # (only what it decodes in a few seconds: its own pages never wait long)
            return not (now - j.queued_at < STATION_DEFER_S and self._gpu_idle(now))
        if j.model and j.model not in w.models and now - j.queued_at < MODEL_DEFER_S and self._model_idle(j, w, now):
            return False                           # (a worker with its model takes it in a moment)
        return j.cls not in FAST_CLASSES or self._fast_ok(w, now)

    def _model_idle(self, j: Job, w: Worker, now: float) -> bool:
        """Another GPU worker has this job's model loaded and could take the job now (in a claim, room for a lease;
        for a pre-alert clip: quick and holding nothing)."""
        return any(o is not w and o.kind == "gpu" and j.model in o.models and o.polling > 0 and self._ready(o, now)
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
                if any(j.cls in FAST_CLASSES for j in self._queue):
                    return False, "a pre-alert clip waits already"
                if not any(self._fast_ok(w, now) for w in idle):
                    svc = min(self._fast_s(w, now) or 0.0 for w in idle)
                    return False, f"slow on pre-alert clips lately ({svc:.1f} s)"
                return True, ""
            if any(w.polling > 0 and len(w.leases) < MAX_LEASES for w in ready) and \
                    not any(j.cls <= cls for j in self._queue):
                return True, ""                    # a worker in a claim now and nothing ahead: it takes it at once
            ahead = sum(1 for j in self._queue if j.cls <= cls) + len(self._leases)
            est = ahead * self._service_s() / len(ready)
        if est > BUDGETS[cls][0]:
            return False, f"busy (about {est:.0f} s of work ahead)"
        return True, ""

    # -- jobs ------------------------------------------------------------------------------------------------------

    def has(self, cid: str) -> bool:
        with self._cv:
            return cid in self._open

    def submit(self, job: Job) -> bool:
        """Queued (True), or not taken: the pool not ready for its class, or the clip already open here. The caller
        goes its own way on False; on True exactly one of on_done / on_fallback follows."""
        ok, _why = self.ready(job.cls, gpu_only=job.gpu_only, origin=str(job.extra.get("origin") or ""),
                              seconds=job.seconds)
        if not ok or self._stop.is_set():
            return False
        with self._cv:
            if job.id in self._open:
                return False
            job.queued_at = self.clock()
            self._queue.append(job)
            self._open.add(job.id)
            self.submitted += 1
            self._cv.notify_all()
        return True

    def _best(self, w: Worker | None = None, now: float | None = None) -> Job | None:
        """The next job for this worker, among those it may take (_fits)."""
        now = self.clock() if now is None else now
        return min((j for j in self._queue if w is None or self._fits(w, j, now)),
                   key=lambda j: (j.cls, j.queued_at), default=None)

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
                            if job.cls in FAST_CLASSES and (self._fast_s(w, now) or 0.0) > PREALERT_MAX_SERVICE_S:
                                w.probe_at = now               # a slow worker's probe: the next one PROBE_S on
                            return self._lease(job, w, now)
                    left = end - time.monotonic()
                    if left <= 0:
                        return None
                    self._cv.wait(min(left, MODEL_DEFER_S / 4 if any(j.model for j in self._queue) else 1.0))
                return None
            finally:
                w.polling -= 1

    def _lease(self, job: Job, w: Worker, now: float) -> Job:
        _wait, lease_s, cap_s = BUDGETS[job.cls]
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
            _wait, lease_s, _cap = BUDGETS[job.cls]
            job.expires = min(job.cap_at, now + lease_s + _extra(job))
            return job

    def _end_lease(self, job: Job, took: float | None = None, lost: bool = False) -> None:
        """The lease over. A pre-alert job's claim -> end goes to its worker's quick gate: took (its text came), or its
        whole cap when the lease was lost (ran out, the worker unheard); a job handed back or taken back on gaming or a
        pause isn't slowness and isn't counted."""
        w = self.workers.get(job.worker)
        if w is not None and job.cls in FAST_CLASSES and job.lease in self._leases and (took is not None or lost):
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
                               now - job.claimed_at if job.claimed_at is not None else None, wid))
            while self._done and now - self._done[0][0] > WINDOW_S:
                self._done.popleft()
            self._cv.notify_all()
        try:
            job.on_done(job, result)
        except Exception as e:  # noqa: BLE001 - its text couldn't be applied: the next engine takes it (never dropped)
            self.log(f"stt pool: {job.id}: the result couldn't be applied ({type(e).__name__}: {e}): falling back")
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
        at once (nobody could take it). How many fell back."""
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
            for job in list(self._queue):
                if not anyone:
                    self._queue.remove(job)
                    self._fall(job, "no_worker", "no pool worker ready")
                    n += 1
                elif now - job.queued_at >= BUDGETS[job.cls][0]:
                    self._queue.remove(job)
                    self._fall(job, "unclaimed", f"no pool worker took it in {BUDGETS[job.cls][0]:g} s")
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
                                "models": list(w.models), "stt_state": dict(w.stt_state),
                                "update": dict(w.update),
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
