"""Local LLM inference on the GPU workers (#639's pool, its own lane): a short JSON classification asked of the model a
worker serves next to its whisper (deploy/stt-worker: llama.cpp's llama-server on 127.0.0.1), never a cloud model.

The contract (the intel engine's, sizeup/intel; it owns the task names, the prompts and the schemas):

    submit(task, messages, schema, deadline_s=20, priority=0, meta=False) -> concurrent.futures.Future
        resolves to a dict that validates against the JSON schema, or None (no worker, gaming, the deadline, an
        invalid answer). meta=True: (dict | None, meta), meta = {model, model_version, worker, latency_ms, tokens_in,
        tokens_out, why: ok | no_worker | deadline | invalid | gaming}, there even when the result is None. Never
        raises, never blocks: with no worker that could answer it is resolved at once (None, no_worker).
    available() -> bool
        a worker advertising an LLM is ready now (off unless one does).

The lane, apart from the speech-to-text queue (sizeup/sttpool.py), which always comes first:
    a worker takes an infer job only while it holds no pre-alert STT lease (Pool.holds_prealert), one at a time, and
    infer leases never count in the STT pool's idle / MAX_LEASES / wait estimates, so nothing here delays a clip's
    routing or fallback. A worker that gets a pre-alert clip while it generates aborts the generation and hands the
    job back (release "preempted"); within its deadline it is leased again, to it or another worker
    jobs        in memory on the hub's leader, highest priority first, then the oldest. Leased until the job's own
                deadline (absolute: no extend). Handed back, the lease lost (its worker unheard LOST_S) or the worker
                gone gaming / paused / its LLM stopped: queued again while MIN_LEFT_S is left, else None
    workers     hello / state / claims carry {state, models: {stt, llm, llm_version}}; llm "" (or none): no LLM now
                (a worker stops its llama-server while gaming)

Switches (Settings > Station > STT workers): the pool's On also turns this lane off (available() False, no claims);
each GPU worker's "LLM jobs" (on by default) keeps prompts off that worker. Only GPU workers' own tokens (sw1) reach
this lane: a station's whisper in the pool never sees a prompt (sizeup/cloud/sttworkers.py answers it 403).

Privacy: the prompts and the answers exist only in memory, on the way to the worker and back to the future. Never logged,
never in Health, stt_runs or a database: only the task, the latency, the token counts and ok / why (stats()). A caller's
done-callback runs on the lane's result thread and must not log the answer either.

The worker API (sizeup/cloud/sttworkers.py routes /api/station/stt/infer/* here, the same token):
    POST /api/station/stt/infer/claim                  {wait <= 25, state, models} -> {job: {lease, id, task, messages,
                                                       schema, max_tokens, temperature, deadline_s} | null}
    POST /api/station/stt/infer/<lease>/complete       {output: {...}, tokens_in, tokens_out, infer_ms} -> {ok}
                                                       (an answer that doesn't validate: None, why invalid)
    POST /api/station/stt/infer/<lease>/release        {why: gaming | preempted | paused | error | stopping} -> {ok}
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

API = "/api/station/stt/infer/"
LEASE_PATH = re.compile(r"/api/station/stt/infer/([0-9a-f]{16})/(complete|release)")
TASK = re.compile(r"[a-z0-9][a-z0-9_.-]{0,39}")
WHYS = ("ok", "no_worker", "deadline", "invalid", "gaming")
STATES = ("online", "gaming", "paused")
ROLES = ("system", "user", "assistant")
DEADLINE_MIN_S, DEADLINE_MAX_S = 1.0, 120.0
MIN_LEFT_S = 1.0           # a job with less than this left is not leased (again): it can't come back in time
MAX_TOKENS = 256           # the answer's tokens at most (a classification: 40-120)
OUTPUT_MAX = 16384         # an answer's JSON, characters at most (a bigger one is invalid)
MESSAGES_MAX = 16
PROMPT_MAX = 32000         # characters, all the messages
SCHEMA_MAX = 8000          # characters, the schema as JSON
READY_S = 15.0             # a worker seen this recently (or in a claim now) is ready (sttpool.READY_S)
OFFLINE_S = 30.0
LOST_S = 12.0              # a lease holder unheard this long loses it (sttpool.LOST_S: two heartbeats)
FAIL_N = 3                 # invalid answers or lost leases in a row: the worker cools down ...
COOLDOWN_S = 120.0         # ... this long (no infer jobs; its STT work is the pool's own business)
CLAIM_WAIT_MAX_S = 25.0
TICK_S = 0.25
WINDOW_S = 7200.0          # done jobs kept for the numbers (Health)

# -- the JSON schema subset (what llama.cpp's json_schema -> grammar takes too) ---------------------------------------

_KEYS = {"type", "enum", "const", "properties", "required", "additionalProperties", "items", "minItems", "maxItems",
         "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "anyOf",
         "title", "description"}
_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")


def supported(schema, depth: int = 0) -> bool:
    """A schema this module can check (and a worker's grammar can follow): the keywords in _KEYS, nested 8 deep at most."""
    if not isinstance(schema, dict) or depth > 8 or set(schema) - _KEYS:
        return False
    t = schema.get("type")
    if t is not None and not all(x in _TYPES for x in ([t] if isinstance(t, str) else t if isinstance(t, list) else [None])):
        return False
    if "enum" in schema and not (isinstance(schema["enum"], list) and schema["enum"]):
        return False
    props = schema.get("properties", {})
    if not isinstance(props, dict) or not all(isinstance(k, str) and supported(v, depth + 1) for k, v in props.items()):
        return False
    req = schema.get("required", [])
    if not isinstance(req, list) or not all(isinstance(k, str) for k in req):
        return False
    if not isinstance(schema.get("additionalProperties", False), bool):
        return False
    if "items" in schema and not supported(schema["items"], depth + 1):
        return False
    if "anyOf" in schema and not (isinstance(schema["anyOf"], list) and schema["anyOf"]
                                  and all(supported(s, depth + 1) for s in schema["anyOf"])):
        return False
    return all(_num(schema[k]) is not None for k in ("minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum",
                                                     "exclusiveMaximum", "minLength", "maxLength") if k in schema)


def _is(v, t: str) -> bool:
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool) or isinstance(v, float) and v.is_integer()
    if t == "number":
        return _num(v) is not None
    return isinstance(v, {"object": dict, "array": list, "string": str}[t])


def valid(v, schema: dict) -> bool:
    """v validates against schema (the subset: supported())."""
    t = schema.get("type")
    if t is not None and not any(_is(v, x) for x in ([t] if isinstance(t, str) else t)):
        return False
    if "enum" in schema and not any(v == e and isinstance(v, bool) == isinstance(e, bool) for e in schema["enum"]):
        return False
    if "const" in schema and v != schema["const"]:
        return False
    if "anyOf" in schema and not any(valid(v, s) for s in schema["anyOf"]):
        return False
    if isinstance(v, dict):
        props = schema.get("properties", {})
        if any(k not in v for k in schema.get("required", [])):
            return False
        if schema.get("additionalProperties", True) is False and set(v) - set(props):
            return False
        if not all(valid(v[k], s) for k, s in props.items() if k in v):
            return False
    if isinstance(v, list):
        if len(v) < schema.get("minItems", 0) or len(v) > schema.get("maxItems", math.inf):
            return False
        if "items" in schema and not all(valid(x, schema["items"]) for x in v):
            return False
    if isinstance(v, str) and not schema.get("minLength", 0) <= len(v) <= schema.get("maxLength", math.inf):
        return False
    if _num(v) is not None:
        if v < schema.get("minimum", -math.inf) or v > schema.get("maximum", math.inf):
            return False
        if "exclusiveMinimum" in schema and v <= schema["exclusiveMinimum"]:
            return False
        if "exclusiveMaximum" in schema and v >= schema["exclusiveMaximum"]:
            return False
    return True


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _int(v, hi: int = 1_000_000) -> int:
    f = _num(v)
    return max(0, min(hi, int(f))) if f is not None else 0


# -- the lane --------------------------------------------------------------------------------------------------------

def _no_constant(name):
    raise ValueError(f"{name} is not JSON")       # NaN / Infinity in an answer: invalid


@dataclass(eq=False)
class Job:
    task: str
    messages: list
    schema: dict
    deadline_at: float
    priority: int
    future: concurrent.futures.Future
    meta: bool
    submitted: float                        # the clock's
    t0: float                               # time.monotonic() at submit: the latency
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    lease: str = ""
    worker: str = ""
    claimed_at: float | None = None
    last_why: str = ""                      # why it came back last (gaming: what None says if it never gets done)
    resolved: bool = False                  # (under the lock: resolved once, whichever path gets there first)


@dataclass
class Worker:
    id: str
    state: str = "online"
    stt: str = ""
    llm: str = ""
    llm_version: str = ""
    llm_state: str = ""                     # its LLM's state ("ok", "unavailable: gave its VRAM to the speech models")
    llm_id: str = ""                        # the llm_models id it runs ("" worker.toml's)
    seen: float = 0.0
    polling: int = 0
    lease: str = ""
    fails: int = 0
    cool_until: float = 0.0
    had_llm: bool = False                   # it advertised an LLM once (Health lists only those; a whisper-only one isn't)


class Lane:
    """The infer queue, leases and numbers. stt_busy(worker id): it holds a pre-alert STT lease now (the pool's);
    may_write(): this task is the leader (a standby answers nothing); enabled(): the admin's pool switch (Settings >
    Station > STT workers: off stops this lane too); llm_on(worker id): that worker's "LLM jobs" switch.

    Futures are resolved on the lane's own result thread (never a worker's request thread, never under a lock); a
    caller's done-callback runs there and must not block or log a prompt or an answer."""

    def __init__(self, stt_busy: Callable[[str], bool] = lambda wid: False,
                 may_write: Callable[[], bool] = lambda: True, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, enabled: Callable[[], bool] = lambda: True,
                 llm_on: Callable[[str], bool] = lambda wid: True):
        self.stt_busy, self.may_write, self.clock, self.log = stt_busy, may_write, clock, log
        self.enabled, self.llm_on = enabled, llm_on
        self.on_queued: Callable[[], None] = lambda: None   # LLM work queued: the pool's challengers step aside (#869)
        self._cv = threading.Condition()
        self._queue: list[Job] = []
        self._leases: dict[str, Job] = {}
        self.workers: dict[str, Worker] = {}
        self._done: deque = deque()          # (at, task, worker, why, latency_ms, tokens_in, tokens_out)
        self._said: list[str] = []           # log lines made under the lock, written after it
        self._switch: dict = {}              # the switches as last read ("": the pool's; a worker id: its LLM jobs)
        self._switch_at = -1e9               # (read outside the lock, at most once a second: the lock never waits on
                                             # hub.sqlite)
        self._switch_gen = 0                 # bumped by switched(): a read begun before it is never written back
        self._switcher: threading.Thread | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # what available() and submit() read without any lock: set under the lock (never any I/O there) whenever a
        # worker, a switch or a lease changes, and by the tick (time: READY_S, cooldowns; the leader, read outside it)
        self._avail = False
        self._nobody = "no_worker"
        self._leader = True
        self._results = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="infer-results")

    def start(self) -> Lane:
        self._refresh()
        self._leader = self._may_write()
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="infer-lane", daemon=True)
            self._thread.start()
            self._switcher = threading.Thread(target=self._switch_loop, name="infer-switches", daemon=True)
            self._switcher.start()
        with self._cv:
            self._recompute(self.clock())
        return self

    def _may_write(self) -> bool:
        try:
            return bool(self.may_write())
        except Exception:  # noqa: BLE001 - can't tell: not the leader
            return False

    def _recompute(self, now: float) -> None:
        """Called with the lock held (no I/O): what available() and submit() read lock-free."""
        self._avail = (self._thread is not None and not self._stop.is_set() and self._leader
                       and any(self._ready(w, now) for w in self.workers.values()))
        self._nobody = "gaming" if any(w.had_llm and w.state == "gaming" and now - w.seen < OFFLINE_S
                                       for w in self.workers.values()) else "no_worker"

    def stop(self) -> int:
        """Every job open now: None at once (no_worker). How many."""
        self._stop.set()
        self._avail = False
        with self._cv:
            jobs = self._queue + list(self._leases.values())
            self._queue, self._leases = [], {}
            for w in self.workers.values():
                w.lease = ""
            self._cv.notify_all()
        for j in jobs:
            self._resolve(j, None, "no_worker")
        return len(jobs)

    def _run(self) -> None:
        """The deadlines: woken at the earliest one (a 15 s job is None by 15 s), else every TICK_S."""
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the lane's clock must never stop
                self.log(f"infer: tick: {type(e).__name__}")
            with self._cv:
                if self._stop.is_set():
                    return
                now = self.clock()
                due = min([j.deadline_at for j in self._leases.values()]
                          + [j.deadline_at - MIN_LEFT_S for j in self._queue], default=now + TICK_S)
                self._cv.wait(max(0.0, min(TICK_S, due - now)))

    def _flush(self) -> None:
        """The log lines made under the lock (never a prompt or an answer)."""
        with self._cv:
            said, self._said = self._said, []
        for line in said:
            self.log(line)

    def _switch_loop(self) -> None:
        """The switches and the leader, read again every second on a thread of their own: hub.sqlite being slow never
        holds the lane's thread (its deadlines) nor a request."""
        while not self._stop.wait(1.0):
            try:
                self._refresh()
                self._leader = self._may_write()
                with self._cv:
                    self._recompute(self.clock())
            except Exception as e:  # noqa: BLE001 - never ends
                self.log(f"infer: switches: {type(e).__name__}")

    def _refresh(self, wids=None) -> None:
        """The switches read again (the pool's, and each worker's or just `wids`'): never with the lane's lock held, so
        hub.sqlite being slow never holds a submit() or a claim. A switch that can't be read: off. A read that began
        before an admin's change (switched()) is dropped, never written over it."""
        def read(fn, *a):
            try:
                return bool(fn(*a))
            except Exception:  # noqa: BLE001 - can't be read: off
                return False
        full = wids is None
        with self._cv:
            gen = self._switch_gen
            if full:
                wids = list(self.workers)
        vals = {"": read(self.enabled), **{w: read(self.llm_on, w) for w in wids}}
        with self._cv:
            if gen != self._switch_gen:
                return                                 # (an admin's change came meanwhile: its own read is newer)
            self._switch.update(vals)
            if full:
                self._switch_at = time.monotonic()
            self._recompute(self.clock())
            self._cv.notify_all()

    def _on(self, wid: str | None = None) -> bool:
        """The admin's switches as last read (the pool on; this worker's LLM jobs on): only cached values, never
        hub.sqlite (asked under the lock). A worker not read yet: off."""
        return bool(self._switch.get("", False)) and (wid is None or bool(self._switch.get(wid, False)))

    def _worker_on(self, wid: str) -> bool:
        """This worker's own "LLM jobs" switch as last read (whatever the pool's)."""
        return bool(self._switch.get(wid, False))

    def switched(self) -> None:
        """An admin changed a switch (WorkerRegistry's listeners): read them again now, so it holds at once (and any
        read begun before is dropped)."""
        with self._cv:
            self._switch_gen += 1
        self._refresh()

    # -- workers ---------------------------------------------------------------------------------------------------

    def seen(self, wid: str, state=None, models=None) -> Worker:
        """Any request of a GPU worker's (the same token as its STT ones): when it was last seen, its state, its models
        (a key it doesn't send is unchanged; llm "" says it has none now). No longer online, or its LLM gone: the job
        it holds back to the queue at once."""
        back = []
        if wid not in self.workers:
            self._refresh([wid])                       # (a worker new here: its switch read before it can claim)
        with self._cv:
            w = self.workers.get(wid)
            if w is None:
                w = self.workers[wid] = Worker(wid)
            w.seen = self.clock()
            if state in STATES:
                w.state = state
            if isinstance(models, dict):
                if isinstance(models.get("stt"), str):
                    w.stt = models["stt"][:80]
                if isinstance(models.get("llm"), str):
                    w.llm = models["llm"][:80]
                    w.llm_version = ""
                if isinstance(models.get("llm_version"), str) and w.llm:
                    w.llm_version = models["llm_version"][:64]
                if isinstance(models.get("llm_state"), str):
                    w.llm_state = models["llm_state"][:80]
                if isinstance(models.get("llm_id"), str):
                    w.llm_id = models["llm_id"][:48]
                w.had_llm = w.had_llm or bool(w.llm) or bool(w.llm_state)
            if w.lease and (w.state != "online" or not w.llm):
                job = self._leases.get(w.lease)
                if job is not None:
                    back.append(self._unlease(job, "gaming" if w.state == "gaming" else "no_worker"))
            self._recompute(w.seen)
            self._cv.notify_all()
        self._finish(back)
        return w

    def forget(self, wid: str) -> None:
        """A worker revoked: gone, its job back to the queue."""
        with self._cv:
            w = self.workers.pop(wid, None)
            job = self._leases.get(w.lease) if w is not None and w.lease else None
            back = [self._unlease(job, "no_worker")] if job is not None else []
            self._recompute(self.clock())
            self._cv.notify_all()
        self._finish(back)

    def any_llm(self) -> bool:
        """A worker has advertised an LLM since the hub started (Health shows the lane)."""
        with self._cv:
            return any(w.had_llm for w in self.workers.values())

    def _ready(self, w: Worker, now: float) -> bool:
        return (w.state == "online" and bool(w.llm) and (w.polling > 0 or now - w.seen < READY_S)
                and now >= w.cool_until and self._on(w.id))

    def available(self) -> bool:
        """A worker advertising an LLM is ready now (the pool and its LLM jobs on, this task the leader). Lock-free: what
        the lane last worked out (a worker's request, a switch, the tick at least every TICK_S), never a lock or the
        database."""
        return self._avail and not self._stop.is_set()

    def _nobody_why(self) -> str:
        """Why no worker can answer: gaming when the LLM's worker games, else no_worker (lock-free, as available())."""
        return self._nobody

    # -- jobs ------------------------------------------------------------------------------------------------------

    def submit(self, task, messages, schema, deadline_s=20, priority=0, meta=False) -> concurrent.futures.Future:
        """An ask it can't take (invalid, nobody ready, the lane stopped) is resolved before this returns, on the
        caller's thread; anything else on the result thread."""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        t0 = time.monotonic()
        try:
            job = self._job(task, messages, schema, deadline_s, priority, bool(meta), fut, t0)
        except Exception:  # noqa: BLE001 - never raises: an ask it can't make is None (invalid)
            self._resolve(Job(str(task)[:40] if isinstance(task, str) else "?", [], {}, 0.0, 0, fut, bool(meta),
                              self.clock(), t0), None, "invalid", inline=True)
            return fut
        if not self.available():
            self._resolve(job, None, self._nobody_why(), inline=True)
            return fut
        with self._cv:
            stopped = self._stop.is_set()
            if not stopped:
                self._queue.append(job)
                self._cv.notify_all()
        if stopped:                                # (stop() ran in between: nothing would ever answer it)
            self._resolve(job, None, "no_worker", inline=True)
        else:
            try:
                self.on_queued()                   # (#869: never held or preempted by a challenger)
            except Exception as e:  # noqa: BLE001 - the ask is queued all the same
                self.log(f"infer: on_queued: {type(e).__name__}")
        return fut

    def llm_busy(self, wid: str) -> bool:
        """LLM work queued, or held by this worker now (#869: no challenger meanwhile). Read without the lane's lock
        (the pool asks under its own: never the two locks at once)."""
        w = self.workers.get(wid)
        return bool(self._queue) or bool(w is not None and w.lease)

    def _job(self, task, messages, schema, deadline_s, priority, meta, fut, t0) -> Job:
        if not isinstance(task, str) or not TASK.fullmatch(task):
            raise ValueError("task")
        if not isinstance(messages, list) or not 0 < len(messages) <= MESSAGES_MAX:
            raise ValueError("messages")
        msgs = []
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in ROLES or not isinstance(m.get("content"), str):
                raise ValueError("messages")
            msgs.append({"role": m["role"], "content": m["content"]})
        if sum(len(m["content"]) for m in msgs) > PROMPT_MAX:
            raise ValueError("messages")
        if not supported(schema) or len(json.dumps(schema)) > SCHEMA_MAX:
            raise ValueError("schema")
        d = _num(deadline_s)
        if d is None:
            raise ValueError("deadline_s")
        now = self.clock()
        return Job(task, msgs, json.loads(json.dumps(schema)), now + max(DEADLINE_MIN_S, min(DEADLINE_MAX_S, d)),
                   int(_num(priority) or 0), fut, meta, now, t0)

    def _best(self, now: float) -> Job | None:
        return min((j for j in self._queue if j.deadline_at - now >= MIN_LEFT_S),
                   key=lambda j: (-j.priority, j.submitted), default=None)

    def claim(self, wid: str, wait: float = 0.0, state=None, models=None) -> Job | None:
        """A worker's long poll: the best job, leased to it until its deadline, or None after `wait`. Only a ready
        worker (the pool and its LLM jobs on) holding no infer job and no pre-alert STT lease gets one."""
        end = time.monotonic() + max(0.0, min(CLAIM_WAIT_MAX_S, _num(wait) or 0.0))
        w = self.seen(wid, state, models)
        with self._cv:
            w.polling += 1
        try:
            while not self._stop.is_set():
                busy = self._busy(wid)                 # (the STT pool's lock: asked before the lane's, never under it)
                with self._cv:
                    now = self.clock()
                    w.seen = now
                    if self.workers.get(wid) is not w:     # revoked meanwhile
                        return None
                    if self._ready(w, now) and not w.lease and self._queue and not busy:
                        job = self._best(now)
                        if job is not None:
                            self._queue.remove(job)
                            job.lease, job.worker, job.claimed_at = secrets.token_hex(8), wid, now
                            self._leases[job.lease] = job
                            w.lease = job.lease
                            return job
                    left = end - time.monotonic()
                    if left <= 0:
                        return None
                    self._cv.wait(min(left, 0.5))   # (the STT side doesn't notify here: a pre-alert lease ending)
            return None
        finally:
            with self._cv:
                w.polling -= 1

    def _busy(self, wid: str) -> bool:
        try:
            return bool(self.stt_busy(wid))
        except Exception:  # noqa: BLE001 - can't tell: STT first
            return True

    @staticmethod
    def public(job: Job, now: float) -> dict:
        """What the worker is told: the prompt and the schema (its llama-server's only), nothing kept."""
        return {"lease": job.lease, "id": job.id, "task": job.task, "messages": job.messages, "schema": job.schema,
                "max_tokens": MAX_TOKENS, "temperature": 0, "deadline_s": round(max(0.0, job.deadline_at - now), 1)}

    def _held(self, wid: str, lease: str) -> Job | None:
        job = self._leases.get(lease)
        return job if job is not None and job.worker == wid and self.clock() < job.deadline_at else None

    def _unlease(self, job: Job, why: str):
        """Called with the lock held: the lease over; back to the queue while MIN_LEFT_S is left -> None, else the
        resolution to make outside the lock -> (job, why)."""
        self._leases.pop(job.lease, None)
        w = self.workers.get(job.worker)
        if w is not None and w.lease == job.lease:
            w.lease = ""
        job.lease, job.worker, job.claimed_at = "", "", None
        job.last_why = why if why == "gaming" else job.last_why
        if job.deadline_at - self.clock() >= MIN_LEFT_S and not self._stop.is_set():
            self._queue.append(job)
            self._cv.notify_all()
            return None
        return job, ("gaming" if job.last_why == "gaming" else "deadline")

    def _failed(self, wid: str, now: float) -> None:
        """Called with the lock held (its log line written after it)."""
        w = self.workers.get(wid)
        if w is not None:
            w.fails += 1
            if w.fails >= FAIL_N:
                w.fails, w.cool_until = 0, now + COOLDOWN_S
                self._said.append(f"infer: GPU worker {wid}: {FAIL_N} failed in a row: no infer jobs for "
                                  f"{COOLDOWN_S:.0f} s")

    @staticmethod
    def _answer(body: dict, schema: dict, size: int | None = None):
        """The worker's output, if it is a JSON object of at most OUTPUT_MAX that validates; else None. Never raises
        (a string that isn't JSON, NaN / Infinity, nested too deep, too big: None). size: the request's bytes (the
        output is in them): over OUTPUT_MAX + the rest of a complete's body, too big without a look inside."""
        try:
            if size is not None and size > OUTPUT_MAX + 1024:
                return None
            out = body.get("output")
            if isinstance(out, str):
                if len(out) > OUTPUT_MAX:
                    return None
                out = json.loads(out, parse_constant=_no_constant)
            if not isinstance(out, dict) or len(json.dumps(out, allow_nan=False)) > OUTPUT_MAX:
                return None
            return out if valid(out, schema) else None
        except Exception:  # noqa: BLE001 - whatever a worker sends: invalid, never a stuck future
            return None

    def complete(self, wid: str, lease: str, body: dict, size: int | None = None) -> bool:
        """The worker's answer for a live lease it holds: the future resolved, with the answer if it validates, else
        None (invalid) (True); a lease gone: False. The answer is parsed and checked outside the lock."""
        with self._cv:
            job = self._held(wid, lease)
            if job is None:
                return False
            schema = job.schema
        out = self._answer(body if isinstance(body, dict) else {}, schema, size)
        with self._cv:
            if self._held(wid, lease) is not job:      # (gone meanwhile: its deadline, the worker revoked)
                return False
            self._leases.pop(lease, None)
            w = self.workers.get(wid)
            if w is not None and w.lease == lease:
                w.lease = ""
            if w is not None:
                if out is not None:
                    w.fails = 0
                else:
                    self._failed(wid, self.clock())
            self._recompute(self.clock())
            self._cv.notify_all()
        self._flush()
        self._resolve(job, out, "ok" if out is not None else "invalid", w,
                      _int(body.get("tokens_in")) if isinstance(body, dict) else 0,
                      _int(body.get("tokens_out")) if isinstance(body, dict) else 0)
        return True

    def release(self, wid: str, lease: str, why: str = "") -> bool:
        """The worker hands the job back (a pre-alert clip came, gaming, its llama-server failing): queued again while
        time is left."""
        with self._cv:
            job = self._held(wid, lease)
            if job is None:
                return False
            if why == "error":
                self._failed(wid, self.clock())
            r = self._unlease(job, "gaming" if why == "gaming" else why)
            self._recompute(self.clock())
        self._flush()
        self._finish([r])
        return True

    def tick(self, now: float | None = None) -> int:
        """Deadlines, lost leases, a worker switched off. Nobody ready at all: what waits is None at once (no_worker /
        gaming). How many were resolved."""
        if self._switcher is None and time.monotonic() - self._switch_at >= 1.0:
            self._refresh()                            # (no switch thread: a lane not started, tests)
            self._leader = self._may_write()
        now = self.clock() if now is None else now
        out = []
        with self._cv:
            for job in list(self._leases.values()):
                w = self.workers.get(job.worker)
                if now >= job.deadline_at:
                    self._leases.pop(job.lease, None)
                    if w is not None and w.lease == job.lease:
                        w.lease = ""
                    self._failed(job.worker, now)
                    out.append((job, "deadline"))
                elif w is None or now - w.seen >= LOST_S:
                    self._failed(job.worker, now)
                    out.append(self._unlease(job, "no_worker"))
                elif not self._on(w.id):                  # the admin switched it (or the pool) off: no more prompts
                    out.append(self._unlease(job, "no_worker"))
            self._recompute(now)
            anyone = any(self._ready(w, now) for w in self.workers.values())
            nobody = "" if anyone else self._nobody
            for job in list(self._queue):
                if not anyone or job.deadline_at - now < MIN_LEFT_S:
                    self._queue.remove(job)
                    out.append((job, nobody or ("gaming" if job.last_why == "gaming" else "deadline")))
            if out:
                self._cv.notify_all()
        self._flush()
        out = [r for r in out if r is not None]
        self._finish(out)
        return len(out)

    def _finish(self, items) -> None:
        for r in items:
            if r is not None:
                self._resolve(r[0], None, r[1])

    def _resolve(self, job: Job, value, why: str, w: Worker | None = None, tin: int = 0, tout: int = 0,
                 inline: bool = False) -> None:
        """Called outside the lock: once per job (whichever path comes first), the number kept (never the prompt or the
        answer), the future set on the result thread (inline: here, the caller's own thread in submit)."""
        ms = int(round((time.monotonic() - job.t0) * 1000))
        meta = {"model": w.llm if w else "", "model_version": w.llm_version if w else "", "worker": w.id if w else "",
                "latency_ms": ms, "tokens_in": tin, "tokens_out": tout, "why": why if why in WHYS else "no_worker"}
        now = self.clock()
        with self._cv:
            if job.resolved:
                return
            job.resolved = True
            self._done.append((now, job.task, meta["worker"], meta["why"], ms, tin, tout))
            while self._done and now - self._done[0][0] > WINDOW_S:
                self._done.popleft()
        result = (value, meta) if job.meta else value
        if inline:
            job.future.set_result(result)
            return
        try:
            self._results.submit(job.future.set_result, result)
        except RuntimeError:                       # (the interpreter shutting down: no thread to be had)
            job.future.set_result(result)

    # -- the worker API (sizeup/cloud/sttworkers.py hands a GPU worker's /api/station/stt/infer/* here) --------------

    def handle(self, wid: str, path: str, d: dict, size: int | None = None) -> tuple[int, dict]:
        if path == API + "claim":
            job = self.claim(wid, d.get("wait"), d.get("state"), d.get("models"))
            return 200, {"job": self.public(job, self.clock()) if job is not None else None, "llm_on": self._on(wid)}
        m = LEASE_PATH.fullmatch(path)
        if m is None:
            return 404, {"error": "not found"}
        if m.group(2) == "complete":
            r = self.complete(wid, m.group(1), d, size)
            return (200, {"ok": True}) if r else (409, {"error": "lease lost"})
        return (200, {"ok": True}) if self.release(wid, m.group(1), str(d.get("why") or "")[:40]) else \
            (409, {"error": "lease lost"})

    # -- the numbers (Health) --------------------------------------------------------------------------------------

    def stats(self) -> dict:
        """Settings > Station > Health: per worker its models and jobs; jobs/h, latency p50/p95, why, per task. Only
        tasks, times, token counts and why."""
        from sizeup.sttpool import pct
        now = self.clock()
        with self._cv:
            hour = [x for x in self._done if now - x[0] <= 3600.0]
            workers = [{"id": w.id, "state": ("offline" if now - w.seen >= OFFLINE_S and w.polling <= 0
                                              else "cooling" if w.state == "online" and now < w.cool_until else w.state),
                        "stt": w.stt, "llm": w.llm, "llm_version": w.llm_version, "busy": bool(w.lease),
                        "llm_state": w.llm_state, "llm_id": w.llm_id,
                        "llm_on": self._worker_on(w.id),
                        "jobs_h": sum(1 for x in hour if x[2] == w.id),
                        "fails_h": sum(1 for x in hour if x[2] == w.id and x[3] != "ok")}
                       for w in sorted(self.workers.values(), key=lambda x: x.id) if w.had_llm]
            queued, leased = len(self._queue), len(self._leases)
            ready = sum(1 for w in self.workers.values() if self._ready(w, now))
        ok = [x[4] / 1000.0 for x in hour if x[3] == "ok"]
        whys: dict[str, int] = {}
        tasks: dict[str, dict] = {}
        for x in hour:
            whys[x[3]] = whys.get(x[3], 0) + 1
            t = tasks.setdefault(x[1], {"n": 0, "ok": 0, "lat": [], "tin": 0, "tout": 0, "why": {}})
            t["n"] += 1
            if x[3] == "ok":
                t["ok"] += 1
                t["lat"].append(x[4] / 1000.0)
                t["tin"] += x[5]
                t["tout"] += x[6]
            else:
                t["why"][x[3]] = t["why"].get(x[3], 0) + 1
        return {"available": self.available(), "on": self._on(), "ready": ready, "queued": queued, "leased": leased,
                "jobs_h": len(hour), "ok_h": len(ok), "p50_s": pct(ok, 50), "p95_s": pct(ok, 95), "why_h": whys,
                "workers": workers,
                "tasks": {k: {"n": t["n"], "ok": t["ok"], "p50_s": pct(t["lat"], 50), "p95_s": pct(t["lat"], 95),
                              "tokens_in": round(t["tin"] / t["ok"]) if t["ok"] else None,
                              "tokens_out": round(t["tout"] / t["ok"]) if t["ok"] else None, "why": t["why"]}
                          for k, t in sorted(tasks.items())}}


# -- the module's API (the intel engine's) -----------------------------------------------------------------------------

_lane: Lane | None = None


def install(lane: Lane) -> None:
    """The hub's lane (HubApp.start)."""
    global _lane
    _lane = lane


def uninstall(lane: Lane) -> None:
    """This lane no longer answers (only if it is the one installed: a newer hub's stays)."""
    global _lane
    if _lane is lane:
        _lane = None


def submit(task: str, messages: list[dict], schema: dict, deadline_s: float = 20, priority: int = 0,
           meta: bool = False) -> concurrent.futures.Future:
    """Ask a GPU worker's LLM: a Future of a dict that validates against schema, or None ((dict | None, meta) with
    meta=True). Never raises, never blocks."""
    lane = _lane
    if lane is not None:
        try:
            return lane.submit(task, messages, schema, deadline_s, priority, meta)
        except Exception:  # noqa: BLE001 - never raises
            pass
    fut: concurrent.futures.Future = concurrent.futures.Future()
    m = {"model": "", "model_version": "", "worker": "", "latency_ms": 0, "tokens_in": 0, "tokens_out": 0,
         "why": "no_worker"}
    fut.set_result((None, m) if meta else None)
    return fut


def available() -> bool:
    """A GPU worker advertising an LLM is ready now."""
    lane = _lane
    try:
        return lane is not None and lane.available()
    except Exception:  # noqa: BLE001 - never raises
        return False
