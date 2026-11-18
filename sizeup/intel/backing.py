"""A local model behind the police pack's rules (admin decisions, 2026-11-17). The rules always run first, unchanged;
only a transmission they left alone is asked about, and a model that isn't there costs nothing: rules only.

    ModelBacking(settings, store=None, clock=time.time, log=print, tz=None, backend=None, worker=False)
        engine.backing = it (sizeup/cad/service.py); the engine calls consider() at the end of every row it reads for
        a pack, and tick() with its own tick

Backends (InferBackend: name, model, available(), classify(messages, schema, deadline_s) -> Future of (dict | None,
meta)); the stage builds the prompt, a backend only carries it:

    PCWorkerBackend   the hub's GPU worker pool (sizeup.cloud.infer, the STT session's): task "pd_classify", deadline
                      DEADLINE_S, meta=True. The admin's gaming PC when it is on and not gaming. Imported on first use:
                      a hub without the module (or without a worker) is simply not available
    BedrockBackend    a STUB: an option for later, off by default (infer_pd_bedrock) with its own hard monthly budget
                      (infer_pd_bedrock_budget_usd, 0); never available, so never asked and never spends. Building it:
                      infer.py's BedrockProvider (Mantle client, prices, the outbound check) is the place to start
    NullBackend       nothing available: rules only (Health says so)
    FakeBackend       tests and tools/intel_replay.py --model fake: no model at all

Which rows (the gate, consider()): the pack named infer_pd_pack ("police": its scope, Port Chester / Rye / Rye Brook
PD, read from the pack, never a talkgroup here); a row the rules raised or changed nothing with (no event, or a
category under its threshold), not suppressed ("disregard", "training") and no routine words, at least
infer_pd_min_words words, asked once per transmission, within infer_pd_hour_cap / infer_pd_day_cap.

What is sent (messages(), never logged): the categories (the pack's own: a category the pack gains, such as "assault",
is offered with no change here), the trigger transmission, up to CONTEXT_MAX transmissions of the same source within
CONTEXT_S (before it; after it too when infer_pd_after_s holds the question that long), and up to three lines of the
site's history (context_bundle.site_lines) when the place heard is a site. Radio IDs (6+ digits) out. Within the
hub's limits (PROMPT_MAX characters, MESSAGES_MAX messages): the context is trimmed oldest first, the trigger kept whole.

The answer (schema "pd_classify" v1, schema()): validated again here (validate()); anything else, None, a deadline,
"no worker", "gaming" is a no-op, counted. Then, under the engine's lock:

    deescalation   true, with an open event of this source in the answer's category (or the only open one on the
                   source, for "none"): a note on that event ("de-escalating (model)"), no new event, no push
    category       not "none", confidence >= infer_pd_threshold (0.7): a hit of rule "model.<backend>" (weight: the
                   confidence) on the row, and the event raised exactly as a rule's (engine._raise: the same lifecycle,
                   place, holds, policy, caps; mode the lowest of the pack's, the category's and infer_pd_mode). Its
                   place: the transcript's, through the locator; else the model's "address" if its words are in the
                   transcript (infer.said), found by the locator, never confirmed. The push title ends in "·m"
                   (push.compose). The model, its version, the confidence and the reason: intel_model, shown on the
                   More info page (context_bundle)

Health (health_row) and Settings > System > Intel (status): the backend in use, available or not, and today's counts
(asked, ok, no_worker, gaming, deadline, invalid, events, notes, capped). Never a prompt or an answer in a log line.
The table intel_model is made on first use (the leader), kept KEEP_S.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from concurrent.futures import Future
from datetime import datetime
from typing import Callable

TASK = "pd_classify"
SCHEMA_VERSION = 1
DEADLINE_S = 15.0
PRIORITY = 0                   # the hub's order among its LLM jobs (highest first); never ahead of a clip's STT
GRACE_S = 10.0                 # a Future still open this long past its deadline: counted "deadline" and forgotten
CONTEXT_S = 120.0              # the same source's transmissions this close to the trigger
CONTEXT_MAX = 5
SOURCE_KEEP = 20               # transmissions remembered per source (for the context)
TEXT_MAX = 600                 # one context transmission, clipped (never the trigger: messages())
PROMPT_MAX = 32000             # the hub's limits (sizeup/cloud/infer.py): characters in all the messages,
MESSAGES_MAX = 16              # ... messages, and the answer's tokens (fixed there: 256, so the reason is short)
HISTORY_MAX = 3
ASKED_MAX = 5000
QUEUE_MAX = 200
KEEP_S = 90 * 86400
PRUNE_S = 3600.0
WEAPONS = ("none", "knife", "gun", "other", "unknown")
KEYS = ("category", "confidence", "in_progress", "weapon", "deescalation", "address", "reason")
ADDRESS_MAX = 80
REASON_MAX = 120
WHYS = ("ok", "no_worker", "gaming", "deadline", "invalid")
COUNTS = ("asked", *WHYS, "events", "notes", "capped", "unavailable")
NOTE = "de-escalating (model)"
RADIO_ID = re.compile(r"\b\d{6,}\b")
SITE_PRECISIONS = ("address", "intersection", "landmark")

SYSTEM = ("You read police radio transcripts (speech-to-text, often misheard) for a fire department that wants an "
          "early warning of calls it may be sent to or must stage for. Answer only from the words given. "
          "category: one of the listed category names when the trigger transmission (with its context) says such a "
          "call, else \"none\" (routine traffic, a unit's status, a call already over). confidence: 0 to 1 that the "
          "category is right. in_progress: the incident is happening now. weapon: the weapon said, \"unknown\" when "
          "one is hinted but not named, else \"none\". deescalation: the transmission says an incident is calming "
          "down or under control (a subject in custody, the party gone, no injuries after all). address: the place as "
          "said in the words (at most 80 characters), or null. reason: at most 120 characters, why.")


# -- the schema and its check -----------------------------------------------------------------------------------------

def schema(categories) -> dict:
    """ "pd_classify" v1: the categories are the pack's, "none" beside them."""
    return {
        "title": TASK,
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": [*categories, "none"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "in_progress": {"type": "boolean"},
            "weapon": {"type": "string", "enum": list(WEAPONS)},
            "deescalation": {"type": "boolean"},
            "address": {"type": ["string", "null"], "maxLength": ADDRESS_MAX},
            "reason": {"type": "string", "maxLength": REASON_MAX},
        },
        "required": list(KEYS),
        "additionalProperties": False,
    }


def validate(d, categories) -> dict | None:
    """The answer as the schema says it, or None (the hub checks it too; this side never trusts that)."""
    if not isinstance(d, dict) or set(d) != set(KEYS):
        return None
    cat, conf = d["category"], d["confidence"]
    if not isinstance(cat, str) or cat not in (*categories, "none"):
        return None
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= float(conf) <= 1.0:
        return None
    if not isinstance(d["in_progress"], bool) or not isinstance(d["deescalation"], bool):
        return None
    if d["weapon"] not in WEAPONS:
        return None
    addr = d["address"]
    if addr is not None and (not isinstance(addr, str) or len(addr) > ADDRESS_MAX):
        return None
    if not isinstance(d["reason"], str) or len(d["reason"]) > REASON_MAX:
        return None
    return {**d, "confidence": float(conf)}


def messages(categories: dict, trigger: dict, context: list[dict], history: list[str]) -> list[dict]:
    """[system, user], within the hub's limits (MESSAGES_MAX messages, PROMPT_MAX characters in all): the context
    trimmed oldest first, then the site history, never the trigger transmission (clipped only if it alone could not
    fit). categories: name -> label; trigger / context: {at, text} (context oldest first, the trigger's own time
    apart); history: up to HISTORY_MAX short lines."""
    def clean(t, n: int | None = TEXT_MAX) -> str:
        t = RADIO_ID.sub("[id]", str(t or "")).strip()
        return t if n is None or len(t) <= n else t[:n - 1] + "…"
    at = float(trigger.get("at") or 0.0)
    head = ["Categories:", *(f"- {k}: {v}" for k, v in categories.items()), ""]
    trig = clean(trigger.get("text"), None)

    def build(ctx: list, hist: list, t: str) -> list[dict]:
        lines = [*head, f"Trigger transmission: {t}"]
        if ctx:
            lines += ["", "Same talkgroup around it (seconds from the trigger, oldest first):"]
            lines += [f"[{float(c['at']) - at:+.0f} s] {clean(c['text'])}" for c in ctx]
        if hist:
            lines += ["", "Earlier calls at the place heard (newest first):", *(f"- {clean(h, 200)}" for h in hist)]
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(lines)}]

    def size(ms: list[dict]) -> int:
        return sum(len(m["content"]) for m in ms)
    ctx, hist = list(context), list(history)[:HISTORY_MAX]
    msgs = build(ctx, hist, trig)
    while size(msgs) > PROMPT_MAX and ctx:
        ctx.pop(0)                             # the oldest context first
        msgs = build(ctx, hist, trig)
    while size(msgs) > PROMPT_MAX and hist:
        hist.pop()                             # then the oldest history line
        msgs = build(ctx, hist, trig)
    over = size(msgs) - PROMPT_MAX
    if over > 0:                               # the trigger alone is too long: its newest end clipped off
        msgs = build(ctx, hist, trig[:max(0, len(trig) - over - 1)] + "…")
    return msgs[:MESSAGES_MAX]


# -- the backends -----------------------------------------------------------------------------------------------------

def _int(v) -> int:
    try:
        return max(0, int(float(v or 0)))
    except (TypeError, ValueError):
        return 0


def meta(why: str, model: str = "", **kw) -> dict:
    return {"model": model, "model_version": "", "worker": "", "latency_ms": 0, "tokens_in": 0, "tokens_out": 0,
            "why": why, **kw}


def resolved(value) -> Future:
    f: Future = Future()
    f.set_result(value)
    return f


class InferBackend:
    """A way to a model: available() never blocks; classify() never raises, never blocks, and its Future resolves to
    (a schema-valid dict or None, meta {model, model_version, worker, latency_ms, tokens_in, tokens_out, why})."""

    name = "none"
    model = ""

    def available(self) -> bool:
        return False

    def classify(self, messages: list[dict], schema: dict, deadline_s: float = DEADLINE_S) -> Future:
        return resolved((None, meta("no_worker")))

    def state(self) -> str:
        """A word for Health: "ok" or why not."""
        return "ok" if self.available() else "unavailable"


class NullBackend(InferBackend):
    """Nothing to ask: rules only."""


class PCWorkerBackend(InferBackend):
    """The hub's GPU worker pool (sizeup.cloud.infer): the admin's gaming PC when it is on and not gaming."""

    name = "pc"

    def __init__(self, module=None):
        self._mod = module                  # tests: a stand-in; else sizeup.cloud.infer on first use
        self._tried = module is not None
        self.model = ""
        self.model_version = ""

    def mod(self):
        if not self._tried:
            self._tried = True
            try:
                from sizeup.cloud import infer as m      # the STT session's (not on every build): imported lazily
                self._mod = m
            except ImportError:
                self._mod = None
        return self._mod

    def available(self) -> bool:
        m = self.mod()
        if m is None:
            return False
        try:
            return bool(m.available())
        except Exception:  # noqa: BLE001 - not there is not available
            return False

    def state(self) -> str:
        return "ok" if self.available() else ("not built here" if self.mod() is None else "unavailable")

    def classify(self, messages, schema, deadline_s=DEADLINE_S) -> Future:
        m = self.mod()
        if m is None:
            return resolved((None, meta("no_worker")))
        try:
            return m.submit(TASK, messages, schema, deadline_s=deadline_s, priority=PRIORITY, meta=True)
        except Exception:  # noqa: BLE001 - "never raises", and if it does: no answer
            return resolved((None, meta("no_worker")))


class BedrockBackend(InferBackend):
    """A STUB, never available (so never chosen, never asked, never billed). Its switch and budget exist in Settings
    so an admin can see it is off; building it means a provider here (infer.py's BedrockProvider to start from) and a
    spend check against infer_pd_bedrock_budget_usd before every call."""

    name = "bedrock"

    def available(self) -> bool:
        return False

    def state(self) -> str:
        return "not built"

    def classify(self, messages, schema, deadline_s=DEADLINE_S) -> Future:
        raise NotImplementedError("the Bedrock backend for pd_classify is a stub")


class FakeBackend(InferBackend):
    """No model: answer(messages) -> a dict or None (or a dict as is). why: the meta's; hold: the Futures stay open
    (a deadline, a late answer: resolve() them). Tests and tools/intel_replay.py --model fake."""

    name = "fake"

    def __init__(self, answer=None, why: str = "ok", up: bool = True, hold: bool = False, model: str = "fake-1"):
        self.answer, self.why, self.up, self.hold, self.model = answer, why, up, hold, model
        self.asked: list[list[dict]] = []
        self.open: list[Future] = []

    def available(self) -> bool:
        return self.up

    def classify(self, messages, schema, deadline_s=DEADLINE_S) -> Future:
        self.asked.append(messages)
        if self.hold:
            f: Future = Future()
            self.open.append(f)
            return f
        d = self.answer(messages) if callable(self.answer) else self.answer
        return resolved((d if self.why == "ok" else None, meta(self.why, self.model, model_version="0000")))


def keyword_answer(categories: dict) -> Callable[[list[dict]], dict]:
    """tools/intel_replay.py --model fake: a category whose label's words (4+ letters) are in the trigger, at 0.75; else
    "none". Only the plumbing is measured with it, never a model."""
    words = {k: {w for w in re.findall(r"[a-z]+", str(v).lower()) if len(w) >= 4} for k, v in categories.items()}

    def answer(msgs: list[dict]) -> dict:
        user = msgs[-1]["content"]
        trig = next((ln for ln in user.splitlines() if ln.startswith("Trigger transmission:")), "")
        have = set(re.findall(r"[a-z]+", trig.lower()))
        cat = next((k for k, ws in words.items() if ws & have), "none")
        return {"category": cat, "confidence": 0.75 if cat != "none" else 0.2, "in_progress": False, "weapon": "none",
                "deescalation": False, "address": None, "reason": "fake: label words"}
    return answer


# -- the stage --------------------------------------------------------------------------------------------------------

TABLE_SQL = ("CREATE TABLE IF NOT EXISTS intel_model (id INTEGER PRIMARY KEY, at REAL NOT NULL, traffic_id TEXT NOT NULL, "
             "event_id INTEGER, pack TEXT NOT NULL, backend TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', "
             "model_version TEXT NOT NULL DEFAULT '', why TEXT NOT NULL, latency_ms INTEGER NOT NULL DEFAULT 0, "
             "category TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0, in_progress INTEGER NOT NULL DEFAULT 0, "
             "weapon TEXT NOT NULL DEFAULT '', deescalation INTEGER NOT NULL DEFAULT 0, address TEXT NOT NULL DEFAULT '', "
             "reason TEXT NOT NULL DEFAULT '', used TEXT NOT NULL DEFAULT '', schema INTEGER NOT NULL DEFAULT 1)")
INDEX_SQL = ("CREATE INDEX IF NOT EXISTS intel_model_at ON intel_model(at)",
             "CREATE INDEX IF NOT EXISTS intel_model_event ON intel_model(event_id)")
SHOWN = ("backend", "model", "model_version", "category", "confidence", "in_progress", "weapon", "deescalation", "reason",
         "at", "used")


def defaults() -> dict:
    from sizeup.intel import infer as infer_mod
    return {k: v for k, v in infer_mod.DEFAULTS.items() if k.startswith("infer_pd")}


class ModelBacking:
    """The stage (the module's doc). settings(): the intel section (infer.py DEFAULTS' infer_pd* keys). worker: the
    answers are applied on the "intel-model" thread (live), never the thread that resolves the Future; False: right
    where they resolve (tests, replay). wait: replay with a real backend: consider() waits for each answer."""

    def __init__(self, settings: Callable[[], dict], store=None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, tz=None, backend: InferBackend | None = None, worker: bool = False,
                 wait: bool = False):
        self.settings, self.store, self.clock, self.log, self.tz = settings, store, clock, log, tz
        self._fixed = backend
        self.pc, self.bedrock, self.null = PCWorkerBackend(), BedrockBackend(), NullBackend()
        self.worker, self.wait = worker, wait
        self.history: Callable | None = None   # history(place, at) -> [line]: context_bundle.site_lines (the CAD sets it)
        self.sources: dict[str, deque] = {}    # source key -> recent {tid, at, text}
        self.asked: dict[str, float] = {}      # traffic id -> when (once per transmission)
        self.pending: list[dict] = []          # jobs held for the context after them (infer_pd_after_s)
        self.open: dict[int, dict] = {}        # id(Future) -> job, until it resolves (or GRACE_S past its deadline)
        self.hour: deque = deque()             # when each question went (the hourly cap)
        self.today = {"day": "", **{k: 0 for k in COUNTS}}
        self.total = {k: 0 for k in COUNTS}    # since start (replay's report: its clock crosses days)
        self.records: list[dict] = []          # without a store (replay, tests)
        self.last_model = {"model": "", "model_version": "", "backend": ""}
        self._lock = threading.Lock()
        self._ready = store is None
        self._pruned = 0.0
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None

    # -- settings, backend, counts --------------------------------------------------------------------------------

    def cfg(self) -> dict:
        out = defaults()
        try:
            out.update({k: v for k, v in (self.settings() or {}).items() if k in out})
        except Exception:  # noqa: BLE001
            pass
        return out

    def backend(self, c: dict | None = None) -> InferBackend:
        """The PC worker when it is there; Bedrock only when an admin turned it on with a budget (and it is built: a
        stub never is); else nothing (rules only)."""
        c = self.cfg() if c is None else c
        if self._fixed is not None:
            return self._fixed if self._fixed.available() else self.null
        if c["infer_pd"] and self.pc.available():
            return self.pc
        if c["infer_pd_bedrock"] and float(c["infer_pd_bedrock_budget_usd"]) > 0 and self.bedrock.available():
            return self.bedrock
        return self.null

    def _roll(self, now: float) -> None:
        day = datetime.fromtimestamp(now, self.tz).strftime("%Y-%m-%d")
        if self.today["day"] != day:
            self.today = {"day": day, **{k: 0 for k in COUNTS}}
        while self.hour and self.hour[0] <= now - 3600:
            self.hour.popleft()

    def count(self, key: str, now: float | None = None) -> None:
        with self._lock:
            self._roll(self.clock() if now is None else now)
            self.today[key] = self.today.get(key, 0) + 1
            self.total[key] = self.total.get(key, 0) + 1

    def status(self) -> dict:
        c = self.cfg()
        b = self.backend(c)
        with self._lock:
            self._roll(self.clock())
            counts = {k: self.today[k] for k in COUNTS}
        on = bool(c["infer_pd"] or c["infer_pd_bedrock"])
        named = self._fixed or self.pc
        return {"on": on, "backend": b.name, "ready": b is not self.null, "model": self.last_model["model"],
                "model_version": self.last_model["model_version"], "pc": named.state(), "bedrock": {
                    "on": bool(c["infer_pd_bedrock"]), "budget_usd": c["infer_pd_bedrock_budget_usd"],
                    "state": self.bedrock.state()}, "today": counts, "cfg": c}

    def health_row(self) -> dict:
        """Settings > Station > Health's Intel line for the model: what is in use, and today's counts."""
        s = self.status()
        t = s["today"]
        if not s["on"]:
            head = "model: off, rules only"
        elif s["ready"]:
            head = f"model: {'PC worker' if s['backend'] == 'pc' else s['backend']} ok"
        else:
            head = f"model: PC worker {s['pc']}, rules only"
        text = f"{head} · today {t['asked']} asked · {t['ok']} answered · {t['events']} events · {t['deadline']} timeouts"
        title = ("The local model behind the police rules (sizeup/intel/backing.py): only rows the rules left alone. "
                 "Today: " + " · ".join(f"{t[k]} {k}" for k in COUNTS)
                 + (f"\nModel {s['model']} {s['model_version']}".rstrip() if s["model"] else "")
                 + f"\nBedrock: {'on' if s['bedrock']['on'] else 'off'} ({s['bedrock']['state']})")
        return {"pack": "model", "label": "Intel model", "on": s["on"], "level": "ok" if s["ready"] else "off",
                "text": text, "title": title}

    # -- the gate ---------------------------------------------------------------------------------------------------

    def consider(self, engine, pack, row: dict, key: str, tid: str, win, at: float, suppressed: bool, hits: list,
                 changed: list) -> list:
        """Every row the engine read for a pack (after the rules): remembered as context, and asked about when it
        passes the gate. The events this made (an inline answer: tests, replay)."""
        c = self.cfg()
        if pack.name != str(c["infer_pd_pack"]) or not (c["infer_pd"] or c["infer_pd_bedrock"]):
            return []
        from sizeup.intel import rules as rules_mod
        text = str(row.get("text") or "")
        src = self.sources.setdefault(key, deque(maxlen=SOURCE_KEEP))
        if text.strip() and not any(x["tid"] == tid for x in src):
            src.append({"tid": tid, "at": at, "text": text})
        out = self._due(engine, at)
        if (suppressed or changed or tid in self.asked or any(r.category == "routine" for r, _w in hits)
                or len(rules_mod.words(text)) < int(c["infer_pd_min_words"])):
            return out
        now = self.clock()
        with self._lock:
            self._roll(now)
            capped = len(self.hour) >= int(c["infer_pd_hour_cap"]) or self.today["asked"] >= int(c["infer_pd_day_cap"])
        if capped:
            self.count("capped", now)
            return out
        if self.backend(c) is self.null:
            self.count("unavailable", now)
            return out
        self.asked[tid] = now
        while len(self.asked) > ASKED_MAX:
            self.asked.pop(next(iter(self.asked)))
        entry = win.entries.get(tid)
        place = entry.place if entry is not None else None
        if place is None:
            place = engine._where(pack, row, key, [text], list(entry.who) if entry is not None else [])
        job = {"pack": pack.name, "key": key, "tid": tid, "row": dict(row), "at": at, "place": place,
               "due": at + float(c["infer_pd_after_s"])}
        if float(c["infer_pd_after_s"]) > 0:
            self.pending.append(job)
            return out
        return out + self._ask(engine, job)

    def _due(self, engine, now: float) -> list:
        """The held jobs whose wait for later context is over."""
        out = []
        for job in [j for j in self.pending if j["due"] <= now]:
            self.pending.remove(job)
            out += self._ask(engine, job)
        return out

    def tick(self, engine, now: float) -> list:
        out = self._due(engine, now)
        for k, job in list(self.open.items()):
            if now - job["sent"] > float(job["deadline"]) + GRACE_S:
                self.open.pop(k, None)
                job["dead"] = True
                self.count("deadline", now)
        if now - self._pruned >= PRUNE_S:
            self._pruned = now
            self.prune(now)
        return out

    # -- asking -----------------------------------------------------------------------------------------------------

    def context(self, key: str, tid: str, at: float) -> list[dict]:
        """Up to CONTEXT_MAX of the source's transmissions within CONTEXT_S of the trigger (the nearest), oldest first."""
        near = [x for x in self.sources.get(key, ()) if x["tid"] != tid and abs(float(x["at"]) - at) <= CONTEXT_S]
        near = sorted(near, key=lambda x: abs(float(x["at"]) - at))[:CONTEXT_MAX]
        return sorted(near, key=lambda x: float(x["at"]))

    def _ask(self, engine, job: dict) -> list:
        c = self.cfg()
        pack = engine.pack(job["pack"])
        b = self.backend(c)
        if pack is None or b is self.null:
            if pack is not None:
                self.count("unavailable")
            return []
        cats = {k: v.get("label") or k for k, v in pack.categories.items()}
        hist: list[str] = []
        p = job["place"]
        if self.history is not None and p is not None and getattr(p, "precision", "") in SITE_PRECISIONS:
            try:
                hist = list(self.history(p, job["at"]) or [])[:HISTORY_MAX]
            except Exception as e:  # noqa: BLE001 - no history is still a question
                self.log(f"intel: model: site history: {type(e).__name__}")
        msgs = messages(cats, {"at": job["at"], "text": job["row"].get("text")}, self.context(job["key"], job["tid"], job["at"]),
                        hist)
        job.update(categories=list(cats), backend=b.name, sent=self.clock(), deadline=DEADLINE_S)
        now = self.clock()
        with self._lock:
            self._roll(now)
            self.hour.append(now)
            self.today["asked"] += 1
            self.total["asked"] += 1
        try:
            fut = b.classify(msgs, schema(list(cats)), DEADLINE_S)
        except Exception as e:  # noqa: BLE001 - a backend that raises (the stub) is no answer
            self.log(f"intel: model: {b.name}: {type(e).__name__}")
            fut = resolved((None, meta("no_worker")))
        if self.wait:
            try:
                fut.result(timeout=DEADLINE_S + GRACE_S)
            except Exception:  # noqa: BLE001
                pass
        if fut.done():
            return self._answered(engine, job, fut)
        self.open[id(fut)] = job
        fut.add_done_callback(lambda f, j=job: self._resolved(engine, j, f))
        return []

    def _resolved(self, engine, job: dict, fut: Future) -> None:
        """On whatever thread resolved it: handed to our own (live), never applied there."""
        self.open.pop(id(fut), None)
        if not self.worker:
            try:
                self._answered(engine, job, fut)
            except Exception as e:  # noqa: BLE001
                self.log(f"intel: model: {type(e).__name__}")
            return
        try:
            self._q.put_nowait((engine, job, fut))
        except queue.Full:
            self.count("deadline")
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="intel-model", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                engine, job, fut = self._q.get(timeout=60.0)
            except queue.Empty:
                return
            try:
                self._answered(engine, job, fut)
            except Exception as e:  # noqa: BLE001 - the thread goes on
                self.log(f"intel: model: {type(e).__name__}")

    # -- the answer -------------------------------------------------------------------------------------------------

    def _answered(self, engine, job: dict, fut: Future) -> list:
        if job.get("dead"):
            return []                          # past its deadline: already counted, never applied late
        try:
            got = fut.result(timeout=0)
        except Exception:  # noqa: BLE001
            got = None
        d, m = got if isinstance(got, tuple) and len(got) == 2 else (got, {})
        m = m if isinstance(m, dict) else {}
        why = str(m.get("why") or ("ok" if d is not None else "invalid"))
        ans = validate(d, job["categories"]) if d is not None else None
        if d is not None and ans is None:
            why = "invalid"
        elif why == "ok" and ans is None:
            why = "invalid"
        self.count(why if why in WHYS else "invalid")
        if m.get("model"):
            self.last_model = {"model": str(m.get("model"))[:80], "model_version": str(m.get("model_version") or "")[:40],
                               "backend": job["backend"]}
        rec = {"at": self.clock(), "traffic_id": job["tid"], "event_id": None, "pack": job["pack"], "backend": job["backend"],
               "model": str(m.get("model") or "")[:80], "model_version": str(m.get("model_version") or "")[:40], "why": why,
               "latency_ms": _int(m.get("latency_ms")),
               "category": "", "confidence": 0.0, "in_progress": 0, "weapon": "", "deescalation": 0, "address": "",
               "reason": "", "used": "", "schema": SCHEMA_VERSION}
        if ans is None:
            self._record(rec)
            return []
        rec.update(category=ans["category"], confidence=round(ans["confidence"], 4), in_progress=int(ans["in_progress"]),
                   weapon=ans["weapon"], deescalation=int(ans["deescalation"]), address=ans["address"] or "",
                   reason=ans["reason"])
        out: list = []
        try:
            out = self._apply(engine, job, ans, rec)
        finally:
            self._record(rec)
        return out

    def _apply(self, engine, job: dict, ans: dict, rec: dict) -> list:
        from sizeup.intel import rules as rules_mod
        from sizeup.intel.model import Hit, lowest
        c = self.cfg()
        if not engine.leader():
            return []
        with engine._lock:
            pack = engine.pack(job["pack"])
            if pack is None:
                return []
            key, tid, at, row = job["key"], job["tid"], job["at"], job["row"]
            cat = ans["category"]
            span = engine._merge_span(pack)
            open_ = engine._open_for(pack, None, key, at, span)
            if ans["deescalation"]:
                mine = [e for e in open_ if e.category == cat] if cat != "none" else (open_ if len(open_) == 1 else [])
                if mine:
                    ev = max(mine, key=lambda e: e.last_at)
                    if ev.note != NOTE:
                        ev.note, ev.updated_at = NOTE, engine.clock()
                        ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
                        engine.events.save(ev)
                        engine.sink.event(ev, "updated")
                        self.count("notes")
                    rec.update(event_id=ev.id, used="note")
                    return [ev]
            if cat == "none" or cat not in pack.categories or ans["confidence"] < float(c["infer_pd_threshold"]):
                return []
            if any(tid in e.traffic_ids for e in open_):
                return []                      # the rules took the row up meanwhile (a read-back placed their event)
            win = engine.windows.get((pack.name, key))
            entry = win.entries.get(tid) if win is not None else None
            if entry is None or entry.suppressed:
                return []                      # spent ("disregard" since) or gone from the window: nothing
            cd = pack.category(cat)
            rule = rules_mod.Rule(f"model.{job['backend']}", 1, cat, round(float(ans["confidence"]), 4), [], [],
                                  str(c["infer_pd_mode"]), True, {})
            entry.hits = [*entry.hits, (rule, rule.w)]     # a new list: the row's own report has its hits already
            if entry.place is None:
                entry.place = job["place"] or self._model_place(engine, pack, row, key, ans, tid)
            dfr = engine._deferred(pack, cat, engine._scores(pack, win), key, at, entry)
            if dfr:
                return []                      # one pre-alert per call: the category it defers to has it
            got = engine._scores(pack, win).get(cat)
            thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
            if got is not None and got[0] >= thr:
                what, contrib, rung = got
            else:                              # the model's confidence passed its own threshold: its event
                what, contrib, rung = rule.w, [(tid, rule, rule.w)], rule.mode
            mode = lowest(pack.mode, cd.get("mode", "push"), rule.mode, rung)
            if mode == "off":
                return []
            ev = engine._raise(pack, row, key, cat, cd, max(what, rule.w), contrib, mode, win, at, tid)
            engine.sink.hit(Hit(tid, rule.id, rule.v, rule.w, cat, pack.name, at, ev.id if ev is not None else None))
            if ev is None:
                return []
            rec.update(event_id=ev.id, used="event")
            self.count("events")
            return [ev]

    def _model_place(self, engine, pack, row, key, ans, tid):
        """The model's "address" only as far as the transcript says it (infer.said), found by the locator, never
        confirmed."""
        from sizeup.intel import infer as infer_mod
        addr = ans.get("address") or ""
        texts = [str(row.get("text") or "")] + [x["text"] for x in self.context(key, tid, float(row.get("start") or 0))]
        loc = infer_mod.said(addr, " ".join(texts)) if addr else ""
        if not loc:
            return None
        place = engine._where(pack, row, key, [loc], [])
        if place is None:
            return None
        place.confirmed = False
        place.radius_m = max(place.radius_m, 150.0)
        place.basis = "; ".join(x for x in (place.basis, "place from the model") if x)
        return place

    # -- the record -------------------------------------------------------------------------------------------------

    def _ensure(self) -> bool:
        if self._ready:
            return True
        if self.store is None:
            return False
        with self.store.lock:
            self.store.db.execute(TABLE_SQL)
            for s in INDEX_SQL:
                self.store.db.execute(s)
        self._ready = True
        return True

    def _record(self, r: dict) -> None:
        if self.store is None:
            self.records.append(r)
            return
        try:
            self._ensure()
            cols = list(r)
            with self.store.lock:              # not a CAD change: no store.rev
                self.store.db.execute(f"INSERT INTO intel_model ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                      [r[k] for k in cols])
        except Exception as e:  # noqa: BLE001 - the record never costs the answer
            self.log(f"intel: model: record: {type(e).__name__}")

    def shown(self, event_id: int) -> dict | None:
        """The model's answer that made (or noted) an event, for the More info page; None: not the model's."""
        if self.store is None:
            r = next((x for x in reversed(self.records) if x.get("event_id") == event_id and x.get("used")), None)
        else:
            if not self._ready:
                if self.store.one("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'intel_model'") is None:
                    return None
                self._ready = True
            r = self.store.one("SELECT * FROM intel_model WHERE event_id = ? AND used != '' ORDER BY at DESC LIMIT 1",
                               (int(event_id),))
        if not r:
            return None
        out = {k: r[k] for k in SHOWN}
        out["in_progress"], out["deescalation"] = bool(out["in_progress"]), bool(out["deescalation"])
        return out

    def prune(self, now: float) -> None:
        if self.store is not None and self._ready:
            with self.store.lock:
                self.store.db.execute("DELETE FROM intel_model WHERE at < ?", (now - KEEP_S,))
