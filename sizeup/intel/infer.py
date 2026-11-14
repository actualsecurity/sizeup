"""The inference stage: an LLM BEHIND the deterministic rules, where they don't do the job (user, 2026-11-12), pluggable,
capped. It backs the rules; it never replaces them.

The rules decide first. Only a candidate they leave UNCERTAIN goes to the model (or a small sample, for evaluation):

    uncertain   a category's WHAT between infer_low (0.3) and the threshold (some words, not enough)
    place       an event the rules raised whose place is not heard or not confirmed (at most once per event per
                PLACE_REASK_S)
    sample      infer_sample_pct of the in-scope transmissions with any hit: asked, recorded, never acted on

The model is asked, as JSON (structured output; the category an enum of the pack's), the category, where (as said), and
a one-line summary with no names. Then:

    category    one more hit, rule "infer.<provider>", weight confidence x infer_weight (0.6). It may push only when the
                model AGREES with the rules: the category the rules were unsure about, the rules' own score for it at
                least infer_agree_min (0.5) and the model's confidence at least infer_agree_conf (0.8), with
                infer_agree_push on. Anything else (a category the model alone brings, a weak rule the model would
                escalate: an MVA into an MVA with injuries) is held to infer_alone_mode ("map"): on the card as the
                model's suggestion ("model: MVA with injuries?"), never a push.
    where       NEVER trusted as said: its street words must be in the transcript, then the gazetteer (place.Locator)
                must find it; even so it is UNCONFIRMED (the push says street and block only, never a house number the
                model gave), and it never replaces a place the rules confirmed
    summary     kept in intel_infer for the console and evaluation only; never in a push

What is sent: the window's transcript with radio IDs (6+ digits) and plate-like tokens taken out, the pack's category
names, the source's towns. Never talkers, roster aliases, radio IDs.

The call never holds the engine: it runs on the "infer" thread (timeout CALL_TIMEOUT_S, no retries), and its answer is
fed back to the engine (IntelEngine._infer_done) under the engine's lock. Without a worker (replay, tests) it is inline.

Providers (Provider: name, run(request) -> Result): "bedrock", Amazon Bedrock in the department's own AWS account (the
police traffic stays in the account; Bedrock doesn't train on prompts), through the Anthropic SDK's Mantle client with
the hub task role, model infer_model (default Claude Haiku 5.5, "anthropic.claude-haiku-5-5"). Another provider: a class
with the same two members, registered in PROVIDERS.

Costs: infer_daily_cap calls a day (200) and infer_budget_usd a month (5), counted from the tokens each call reports at
the model's price: PRICES (known ids), or infer_usd_in / infer_usd_out set in Settings for another id (an unknown id with
no price is refused, so the budget can't be bypassed by changing the model). A call is roughly 1,000-1,500 tokens in
(the instructions, the categories, up to 4,000 characters of transcript) and 100-400 out (the JSON, and the model's
brief thinking at effort low): about $0.0003 on Haiku 5.5, so 200 calls a day is about $2 a month.

Off by default (infer = false) and behind that flag until sizeup-aws/26-intel-bedrock.sh has run. The table intel_infer is
made on first use, on the leader only, never at start.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

# $ per million tokens (in, out). Haiku 5.5's list price; Bedrock's may differ: Settings' infer_usd_in / _out win.
PRICES = {"anthropic.claude-haiku-5-5": (0.10, 0.50), "us.anthropic.claude-haiku-5-5": (0.10, 0.50),
          "global.anthropic.claude-haiku-5-5": (0.10, 0.50), "anthropic.claude-sonnet-5-5": (2.0, 10.0),
          "us.anthropic.claude-sonnet-5-5": (2.0, 10.0), "anthropic.claude-opus-5-5": (4.0, 20.0)}
DEFAULTS = {"infer": False, "infer_provider": "bedrock", "infer_model": "anthropic.claude-haiku-5-5",
            "infer_region": "us-east-1", "infer_daily_cap": 200, "infer_budget_usd": 5.0, "infer_low": 0.3,
            "infer_weight": 0.6, "infer_mode": "push", "infer_alone_mode": "map", "infer_agree_push": True,
            "infer_agree_min": 0.5, "infer_agree_conf": 0.8,
            "infer_sample_pct": 0, "infer_usd_in": None, "infer_usd_out": None}
LIMITS = {"infer_daily_cap": (0, 10000), "infer_budget_usd": (0, 500), "infer_low": (0, 1), "infer_weight": (0, 1),
          "infer_sample_pct": (0, 100), "infer_usd_in": (0, 100), "infer_usd_out": (0, 500), "infer_agree_min": (0, 1),
          "infer_agree_conf": (0, 1)}
BACKOFF_S = 600.0
CALL_TIMEOUT_S = 12.0
PLACE_REASK_S = 600.0
MAX_TOKENS = 2000
TEXT_MAX = 4000
QUEUE_MAX = 50
RADIO_ID = re.compile(r"\b\d{6,}\b")
PLATE = re.compile(r"\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{5,8}\b|\b[A-Za-z]{2,4}[\s-]\d{3,4}\b")
SYSTEM = ("You read police and fire radio dispatch transcripts (speech-to-text, often misheard) for a fire department's "
          "pre-alerts. Answer only from the words given. category: one of the listed category names, or \"none\" when "
          "the words don't say one. confidence: 0 to 1. location: the place exactly as said in the transcript (a street "
          "address, two streets that meet, a block, a named place or a highway exit); \"\" when no place is said; never "
          "a place the words don't name. summary: at most 15 words saying what happened and where, with NO person's "
          "name, plate, date of birth, phone number, radio ID or unit callsign.")
STREET_WORDS = {"st", "street", "ave", "avenue", "av", "rd", "road", "pl", "place", "ln", "lane", "dr", "drive", "blvd",
                "ct", "court", "pkwy", "parkway", "ter", "terrace", "way", "and", "the", "of", "at", "block", "hundred",
                "corner", "north", "south", "east", "west", "n", "s", "e", "w"}


def schema(categories) -> dict:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": [*categories, "none"]},
            "confidence": {"type": "number"},
            "location_kind": {"type": "string", "enum": ["address", "intersection", "block", "landmark", "highway", "none"]},
            "location": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["category", "confidence", "location_kind", "location", "summary"],
        "additionalProperties": False,
    }


@dataclass
class Request:
    text: str
    categories: dict          # name -> label
    towns: list
    source: str = ""


@dataclass
class Result:
    category: str = "none"
    confidence: float = 0.0
    location_kind: str = "none"
    location: str = ""
    summary: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    refused: bool = False
    raw: dict = field(default_factory=dict)
    record_id: int | None = None


def clean_text(text: str) -> str:
    """What may be sent: radio IDs and plate-like tokens out, at most TEXT_MAX characters (the newest end kept)."""
    t = PLATE.sub("[plate]", RADIO_ID.sub("[id]", text or ""))
    return t[-TEXT_MAX:]


def said(location: str, text: str) -> str:
    """The model's location as far as the transcript says it: "" when a telling word of it (not the street type) is
    not in the transcript; without its house number when that number isn't (the street alone)."""
    words = {w for w in re.findall(r"[a-z]+", (location or "").lower()) if len(w) >= 3 and w not in STREET_WORDS}
    have = set(re.findall(r"[a-z]+", (text or "").lower()))
    if not words or not words <= have:
        return ""
    loc = (location or "").strip()
    m = re.match(r"^\s*(\d+[A-Za-z]?)\s+(.*)$", loc)
    if m and m.group(1).lower() not in set(re.findall(r"\d+[a-z]?", (text or "").lower())):
        loc = m.group(2)                      # a number the transcript never said: dropped
    return loc


def prompt(req: Request) -> str:
    cats = "\n".join(f"- {k}: {v}" for k, v in req.categories.items())
    return (f"Categories:\n{cats}\n\nTowns: {', '.join(req.towns) or 'any'}\n\n"
            f"Transcript (oldest first, \" . \" between transmissions):\n{req.text}")


class BedrockProvider:
    """Claude on Amazon Bedrock (the department's own account), through the Anthropic SDK's Mantle client with the
    task role's credentials, CALL_TIMEOUT_S and no retries. The SDK (anthropic[bedrock]) is imported on first use."""

    name = "bedrock"

    def __init__(self, model: str, region: str):
        self.model, self.region = model, region
        self._client = None

    def client(self):
        if self._client is None:
            try:
                from anthropic import AnthropicBedrockMantle
            except ImportError as e:
                raise RuntimeError("the Anthropic SDK isn't installed (pip install 'anthropic[bedrock]')") from e
            self._client = AnthropicBedrockMantle(aws_region=self.region, timeout=CALL_TIMEOUT_S, max_retries=0)
        return self._client

    def run(self, req: Request) -> Result:
        response = self.client().messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt(req)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema(req.categories)}},
        )
        usage = getattr(response, "usage", None)
        tin, tout = int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)
        if response.stop_reason == "refusal":
            return Result(refused=True, tokens_in=tin, tokens_out=tout)
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        return parse(text, tin, tout)


def parse(text: str, tin: int = 0, tout: int = 0) -> Result:
    try:
        d = json.loads(text or "{}")
    except ValueError:
        return Result(tokens_in=tin, tokens_out=tout)
    if not isinstance(d, dict):
        return Result(tokens_in=tin, tokens_out=tout)
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        conf = 0.0
    return Result(category=str(d.get("category") or "none")[:40], confidence=conf,
                  location_kind=str(d.get("location_kind") or "none")[:20], location=str(d.get("location") or "")[:160],
                  summary=str(d.get("summary") or "")[:200], tokens_in=tin, tokens_out=tout, raw=d)


PROVIDERS: dict[str, Callable[[dict], object]] = {
    "bedrock": lambda cfg: BedrockProvider(str(cfg["infer_model"]), str(cfg["infer_region"])),
}

TABLE_SQL = ("CREATE TABLE IF NOT EXISTS intel_infer (id INTEGER PRIMARY KEY, at REAL NOT NULL, traffic_id TEXT NOT NULL, "
             "event_id INTEGER, pack TEXT NOT NULL, why TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, "
             "category TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0, location_kind TEXT NOT NULL DEFAULT '', "
             "location TEXT NOT NULL DEFAULT '', placed TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', "
             "tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, usd REAL NOT NULL DEFAULT 0, "
             "used INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')")
INDEX_SQL = "CREATE INDEX IF NOT EXISTS intel_infer_at ON intel_infer(at)"
KEEP_S = 90 * 86400


def price(c: dict) -> tuple[float, float] | None:
    """$ per million tokens (in, out) for the configured model: Settings' own, else PRICES; None: unknown (refused)."""
    if c.get("infer_usd_in") is not None and c.get("infer_usd_out") is not None:
        return float(c["infer_usd_in"]), float(c["infer_usd_out"])
    return PRICES.get(str(c.get("infer_model") or ""))


class Inference:
    """The stage: when to ask, the caps, the provider, the record. settings(): the intel section (DEFAULTS' keys).
    worker: the calls on the "infer" thread (live); False: inline (replay, tests)."""

    def __init__(self, settings: Callable[[], dict], store=None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, provider=None, tz=None, worker: bool = False):
        self.settings, self.store, self.clock, self.log, self.tz = settings, store, clock, log, tz
        self._provider = provider                 # tests / replay: a fake; else made from the settings
        self._made_for: tuple = ()
        self._made = None
        self.usage = {"day": "", "calls": 0, "month": "", "usd": 0.0}
        self.records: list[dict] = []             # without a store (replay): kept here
        self.failed_at = 0.0
        self.why = ""
        self._ready = store is None               # the table: made on first use (the leader's first call)
        self._lock = threading.Lock()
        self.worker = worker
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self.asked: dict[int, float] = {}         # event id -> when its place was last asked about

    def cfg(self) -> dict:
        out = dict(DEFAULTS)
        try:
            out.update({k: v for k, v in (self.settings() or {}).items() if k in DEFAULTS})
        except Exception:  # noqa: BLE001
            pass
        return out

    def provider(self, c: dict):
        if self._provider is not None:
            return self._provider
        key = (c["infer_provider"], c["infer_model"], c["infer_region"])
        if self._made_for != key:
            make = PROVIDERS.get(str(c["infer_provider"]))
            self._made = make(c) if make else None
            self._made_for = key
        return self._made

    def _ensure(self) -> None:
        """The table and the usage so far, on first use (never at start; the caller is the leader)."""
        if self._ready or self.store is None:
            return
        with self.store.lock:
            self.store.db.execute(TABLE_SQL)
            self.store.db.execute(INDEX_SQL)
        try:
            self.usage.update(json.loads(self.store.meta_get("intel_infer_usage") or "{}"))
        except (ValueError, TypeError):
            pass
        self._ready = True

    def _roll(self, now: float) -> None:
        d = datetime.fromtimestamp(now, self.tz)
        day, month = d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")
        if self.usage.get("day") != day:
            self.usage.update(day=day, calls=0)
        if self.usage.get("month") != month:
            self.usage.update(month=month, usd=0.0)

    def allowed(self, now: float | None = None) -> tuple[bool, str]:
        c = self.cfg()
        now = self.clock() if now is None else now
        if not c["infer"]:
            return False, "off"
        if price(c) is None:
            return False, f"no price for {c['infer_model']}: set infer_usd_in / infer_usd_out"
        if self.failed_at and now - self.failed_at < BACKOFF_S:
            return False, self.why or "backing off"
        self._roll(now)
        if self.usage["calls"] >= int(c["infer_daily_cap"]):
            return False, "daily cap"
        if float(self.usage["usd"]) >= float(c["infer_budget_usd"]):
            return False, "monthly budget"
        return True, ""

    def status(self) -> dict:
        ok, why = self.allowed()
        c = self.cfg()
        return {"on": bool(c["infer"]), "ready": ok, "why": why, "provider": c["infer_provider"], "model": c["infer_model"],
                "calls_today": self.usage.get("calls", 0), "usd_month": round(float(self.usage.get("usd", 0.0)), 4),
                "daily_cap": c["infer_daily_cap"], "budget_usd": c["infer_budget_usd"], "price": price(c)}

    def submit(self, req: Request, job: dict, done: Callable[[dict, Result], None]) -> bool:
        """Ask within the caps: on the infer thread (live) or here (replay); done(job, result) gets the answer.
        False: not asked (off, a cap, backing off, the queue full)."""
        now = self.clock()
        if not self.allowed(now)[0]:
            return False                          # off (or capped): no table, no usage read, nothing at all
        self._ensure()                            # the leader's first call: the table, and the usage so far
        with self._lock:
            ok, _why = self.allowed(now)
            if not ok:
                return False
            self.usage["calls"] = int(self.usage["calls"]) + 1
        if not self.worker:
            self._call(req, job, done)
            return True
        try:
            self._q.put_nowait((req, job, done))
        except queue.Full:
            return False
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="infer", daemon=True)
            self._thread.start()
        return True

    def _run(self) -> None:
        while True:
            try:
                req, job, done = self._q.get(timeout=60.0)
            except queue.Empty:
                return
            try:
                self._call(req, job, done)
            except Exception as e:  # noqa: BLE001 - the thread goes on
                self.log(f"intel: inference: {type(e).__name__}")
            finally:
                self._q.task_done()

    def flush(self, timeout: float = 10.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    def _call(self, req: Request, job: dict, done) -> None:
        c = self.cfg()
        now = self.clock()
        p = self.provider(c)
        if p is None:
            self.failed_at, self.why = now, f"no provider {c['infer_provider']!r}"
            return
        req = Request(clean_text(req.text), req.categories, req.towns, req.source)
        err = ""
        try:
            res = p.run(req)
        except Exception as e:  # noqa: BLE001 - a model that fails never stops the rules
            self.failed_at, self.why = now, f"{type(e).__name__}: {str(e)[:120]}"
            self.log(f"intel: inference failed ({type(e).__name__}): backing off {int(BACKOFF_S)} s")
            res, err = None, self.why
        if res is not None:
            self.failed_at, self.why = 0.0, ""
        usd = 0.0
        pr = price(c) or (0.0, 0.0)
        if res is not None:
            usd = (res.tokens_in * pr[0] + res.tokens_out * pr[1]) / 1e6
            with self._lock:
                self.usage["usd"] = float(self.usage["usd"]) + usd
        rec = {"at": now, "traffic_id": job.get("tid", ""), "event_id": job.get("event_id"), "pack": job.get("pack", ""),
               "why": job.get("why", ""), "provider": getattr(p, "name", c["infer_provider"]), "model": c["infer_model"],
               "category": res.category if res else "", "confidence": res.confidence if res else 0.0,
               "location_kind": res.location_kind if res else "", "location": res.location if res else "",
               "summary": res.summary if res else "", "tokens_in": res.tokens_in if res else 0,
               "tokens_out": res.tokens_out if res else 0, "usd": usd, "error": err}
        self._record(rec)
        if res is not None:
            res.record_id = rec.get("id")
            done(job, res)

    def _record(self, r: dict) -> None:
        if self.store is None:
            self.records.append(r)
            return
        self._ensure()
        cols = list(r)
        with self.store.lock:                     # not a CAD change: no store.rev
            cur = self.store.db.execute(f"INSERT INTO intel_infer ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                        [r[k] for k in cols])
            r["id"] = int(cur.lastrowid)
        try:
            self.store.meta_put("intel_infer_usage", json.dumps(self.usage))
        except Exception:  # noqa: BLE001
            pass

    def mark_used(self, rec_id: int | None, placed: str) -> None:
        if self.store is None or rec_id is None or not self._ready:
            return
        with self.store.lock:
            self.store.db.execute("UPDATE intel_infer SET used = 1, placed = ? WHERE id = ?", (placed[:160], rec_id))

    def prune(self, now: float) -> None:
        if self.store is not None and self._ready:
            with self.store.lock:
                self.store.db.execute("DELETE FROM intel_infer WHERE at < ?", (now - KEEP_S,))
