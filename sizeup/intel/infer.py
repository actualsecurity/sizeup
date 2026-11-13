"""The inference stage: an LLM where the deterministic rules don't do the job (user, 2026-11-12), pluggable, capped.

The rules decide first. Only a candidate they leave UNCERTAIN goes to the model (or a small sample, for evaluation):

    uncertain   a category's WHAT between infer_low (0.3) and the threshold (some words, not enough), or an event the
                rules raised whose place is still not heard or not confirmed
    sample      infer_sample_pct of the in-scope transmissions with any hit: run, recorded, never acted on

The model is asked three things about the window's words, as JSON (structured output): the category (one of the pack's,
or none), where (an address, an intersection, a landmark, as said), and a one-line summary with no names. Then:

    category    must be one of the pack's categories, else ignored; it is a hit of rule "infer.<provider>" with weight
                confidence x infer_weight (0.6), counted like any rule's (mode infer_mode: the ladder caps it)
    where       NEVER trusted as said: it goes through the gazetteer (place.Locator) like any transcript, and only a
                place the gazetteer finds is used (then unconfirmed unless the locator confirms it)
    summary     kept in intel_infer for the console (hover) and evaluation only; never in a push (only allow-listed
                fields go in a push, sizeup/intel/push.py)

What is sent: the window's transcript with radio IDs (6+ digits) and plate-like tokens taken out, the pack's category
names, the source's towns. Never talkers, roster aliases, radio IDs.

Providers (Provider: name, run(request) -> Result): "bedrock", Amazon Bedrock in the department's own AWS account (the
police traffic stays in the account; Bedrock doesn't train on prompts), through the Anthropic SDK's Mantle client with
the hub task role, model infer_model (default Claude Haiku 5.5, "anthropic.claude-haiku-5-5"). Another provider: a class
with the same two members, registered in PROVIDERS.

Off by default (infer = false) and behind the feature flag until sizeup-aws/26-intel-bedrock.sh has granted the task
role bedrock-mantle / bedrock InvokeModel on that model. Costs are capped: infer_daily_cap calls a day (200) and
infer_budget_usd a month (5), counted from the tokens each call reports at infer_usd_in / infer_usd_out per million
(Haiku 5.5: $0.10 / $0.50; Bedrock's own price may differ: set them in Settings). Over either cap: no calls until the
day / month turns. A provider that fails backs off (BACKOFF_S) and says why in Settings > System > Intel.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

DEFAULTS = {"infer": False, "infer_provider": "bedrock", "infer_model": "anthropic.claude-haiku-5-5",
            "infer_region": "us-east-1", "infer_daily_cap": 200, "infer_budget_usd": 5.0, "infer_low": 0.3,
            "infer_weight": 0.6, "infer_mode": "push", "infer_sample_pct": 0, "infer_usd_in": 0.10, "infer_usd_out": 0.50}
LIMITS = {"infer_daily_cap": (0, 10000), "infer_budget_usd": (0, 500), "infer_low": (0, 1), "infer_weight": (0, 1),
          "infer_sample_pct": (0, 100), "infer_usd_in": (0, 100), "infer_usd_out": (0, 500)}
BACKOFF_S = 600.0
MAX_TOKENS = 2000
TEXT_MAX = 4000
RADIO_ID = re.compile(r"\b\d{6,}\b")
PLATE = re.compile(r"\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{5,8}\b")
SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "confidence": {"type": "number"},
        "location_kind": {"type": "string", "enum": ["address", "intersection", "block", "landmark", "highway", "none"]},
        "location": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["category", "confidence", "location_kind", "location", "summary"],
    "additionalProperties": False,
}
SYSTEM = ("You read police and fire radio dispatch transcripts (speech-to-text, often misheard) for a fire department's "
          "pre-alerts. Answer only from the words given. category: exactly one of the listed category names, or \"none\" "
          "when the words don't say one. confidence: 0 to 1. location: the place as said (a street address, two streets "
          "that meet, a block, a named place or a highway exit), corrected only for obvious mishearing of a listed "
          "town's streets; \"\" when no place is said. summary: at most 15 words saying what happened and where, with "
          "NO person's name, plate, date of birth, phone number, radio ID or unit callsign.")


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


def prompt(req: Request) -> str:
    cats = "\n".join(f"- {k}: {v}" for k, v in req.categories.items())
    return (f"Categories:\n{cats}\n\nTowns: {', '.join(req.towns) or 'any'}\n\n"
            f"Transcript (oldest first, \" . \" between transmissions):\n{req.text}")


class BedrockProvider:
    """Claude on Amazon Bedrock (the department's own account), through the Anthropic SDK's Mantle client with the
    task role's credentials. The SDK (anthropic[bedrock]) is imported on first use: without it, a clear error."""

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
            self._client = AnthropicBedrockMantle(aws_region=self.region)
        return self._client

    def run(self, req: Request) -> Result:
        response = self.client().messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt(req)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
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

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS intel_infer (
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  traffic_id TEXT NOT NULL,                -- the transmission that asked
  event_id INTEGER,
  pack TEXT NOT NULL, why TEXT NOT NULL,   -- uncertain | place | sample
  provider TEXT NOT NULL, model TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0,
  location_kind TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', placed TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, usd REAL NOT NULL DEFAULT 0,
  used INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS intel_infer_at ON intel_infer(at);
"""
KEEP_S = 90 * 86400


class Inference:
    """The stage: when to ask, the caps, the provider, the record. settings(): the intel section (DEFAULTS' keys)."""

    def __init__(self, settings: Callable[[], dict], store=None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print, provider=None, tz=None):
        self.settings, self.store, self.clock, self.log, self.tz = settings, store, clock, log, tz
        self._provider = provider                 # tests / replay: a fake; else made from the settings
        self._made_for: tuple = ()
        self.usage = {"day": "", "calls": 0, "month": "", "usd": 0.0}
        self.records: list[dict] = []             # without a store (replay): kept here
        self.failed_at = 0.0
        self.why = ""
        self._lock = threading.Lock()
        if store is not None:
            with store.lock:
                store.db.executescript(SCHEMA_SQL)
            try:
                self.usage.update(json.loads(store.meta_get("intel_infer_usage") or "{}"))
            except (ValueError, TypeError):
                pass

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
                "daily_cap": c["infer_daily_cap"], "budget_usd": c["infer_budget_usd"]}

    def ask(self, req: Request, *, tid: str, pack: str, why: str, event_id: int | None = None) -> Result | None:
        """One call within the caps (None: not allowed now, or it failed). Recorded either way it ran."""
        now = self.clock()
        with self._lock:
            ok, _why = self.allowed(now)
            if not ok:
                return None
            c = self.cfg()
            self.usage["calls"] = int(self.usage["calls"]) + 1
        p = self.provider(c)
        if p is None:
            self.failed_at, self.why = now, f"no provider {c['infer_provider']!r}"
            return None
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
        if res is not None:
            usd = (res.tokens_in * float(c["infer_usd_in"]) + res.tokens_out * float(c["infer_usd_out"])) / 1e6
            with self._lock:
                self.usage["usd"] = float(self.usage["usd"]) + usd
        rec = {"at": now, "traffic_id": tid, "event_id": event_id, "pack": pack, "why": why,
                      "provider": getattr(p, "name", c["infer_provider"]), "model": c["infer_model"],
                      "category": res.category if res else "", "confidence": res.confidence if res else 0.0,
                      "location_kind": res.location_kind if res else "", "location": res.location if res else "",
                      "summary": res.summary if res else "", "tokens_in": res.tokens_in if res else 0,
                      "tokens_out": res.tokens_out if res else 0, "usd": usd, "error": err}
        self._record(rec)
        if res is not None:
            res.record_id = rec.get("id")
        return res

    def _record(self, r: dict) -> None:
        if self.store is None:
            self.records.append(r)
            return
        cols = list(r)
        with self.store.tx() as db:
            cur = db.execute(f"INSERT INTO intel_infer ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [r[k] for k in cols])
            r["id"] = int(cur.lastrowid)
        try:
            self.store.meta_put("intel_infer_usage", json.dumps(self.usage))
        except Exception:  # noqa: BLE001
            pass

    def mark_used(self, rec_id: int | None, placed: str) -> None:
        if self.store is None or rec_id is None:
            return
        with self.store.tx() as db:
            db.execute("UPDATE intel_infer SET used = 1, placed = ? WHERE id = ?", (placed[:160], rec_id))

    def prune(self, now: float) -> None:
        if self.store is not None:
            with self.store.tx() as db:
                db.execute("DELETE FROM intel_infer WHERE at < ?", (now - KEEP_S,))
