"""Per-channel speech-to-text models on the GPU workers (#639): the catalog, each channel's model and context, the models
the workers are asked to run, and the context each clip carries.

    catalog     tools/stt_worker/pins.json "stt_models" (the worker checks it whole: worker.py check_catalog): each model's
                kind (whisper-ggml: whisper-server, its context a prompt; llama-asr: llama-server with an audio model,
                its context a short list), VRAM, services and context_default. The hub tells the workers which catalog
                (word(): the commit it was built from and pins.json's SHA-256) and which models it wants (want()).
    choices     hub.sqlite meta "stt_models" (JSON {channel key: {model?, context?, callsigns?}}): Settings > Radios >
                <channel> > Speech model, admins, audited. Not set: DEFAULTS by the channel's service (fire, EMS and
                police: Qwen3-ASR; airband: the ATC whisper), FALLBACK for any other. context "on" | "off" over the
                model's context_default (the ATC whisper's is off: a prompt raised its repetition loops). callsigns: the
                channel's fixed call signs ("908", "Headquarters": a dispatch console's), said all the time, in every
                clip's context whoever talks.
    context     context_list(): what the GPU worker's llama-asr model gets as its system message: spoken forms, ranked,
                at most CONTEXT_MAX, nothing from the clip's own words (who keyed up by name, the channel's call signs,
                the aircraft near the field, then the channel's terms). A whisper model gets the station's own prompt
                (App._prompt_v), the same names in it.

Never more than names: a radio's raw ID is never in any context (sizeup/talkers.py hint_names)."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
PINS = ROOT / "tools" / "stt_worker" / "pins.json"
META = "stt_models"
KINDS = ("whisper-ggml", "llama-asr")
ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
FALLBACK = "large-v3-turbo-q5_0"              # today's whisper (every worker's whisper_server)
DEFAULTS = {"fire": "qwen3-asr-1.7b-q8_0", "ems": "qwen3-asr-1.7b-q8_0", "police": "qwen3-asr-1.7b-q8_0",
            "air": "large-v3-turbo-us-atc-v3-f16"}
SERVICE_ORDER = ("fire", "ems", "police", "air", "transit", "other", "")   # whose model is loaded first (VRAM)
CONTEXT_MAX = 12
CALLSIGNS_MAX = 8
CALLSIGN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .'/-]{0,39}")
WANT_MAX = 8
FIELDS = ("model", "context", "callsigns", "challenger", "pct", "promoted", "rolled_back")
PCT_DEFAULT = 10.0           # a challenger's share of its channel's clips


def load(path: Path | str = PINS) -> tuple[dict, str]:
    """(the catalog {id: {kind, vram_mb, services, context, context_default, what}}, pins.json's SHA-256); ({}, "") when
    it can't be read (no per-channel models then: every clip as before)."""
    try:
        raw = Path(path).read_bytes()
        doc = json.loads(raw)
    except (OSError, ValueError):
        return {}, ""
    out = {}
    for mid, e in (doc.get("stt_models") or {}).items() if isinstance(doc, dict) else ():
        if mid.startswith("_") or not ID.fullmatch(mid) or not isinstance(e, dict) or e.get("kind") not in KINDS:
            continue
        out[mid] = {"id": mid, "kind": e["kind"], "vram_mb": int(e.get("vram_mb") or 0),
                    "services": [s for s in e.get("services") or [] if isinstance(s, str)],
                    "context": "list" if e["kind"] == "llama-asr" else "prompt",
                    "context_default": bool(e.get("context_default", True)), "what": str(e.get("what") or "")[:120]}
    return out, hashlib.sha256(raw).hexdigest()


def clean_callsigns(v) -> list[str]:
    if v in (None, ""):
        return []
    if isinstance(v, str):
        v = [x for x in v.split(",")]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError("callsigns: a list of call signs")
    out = []
    for x in v:
        x = " ".join(x.split())
        if not x:
            continue
        if not CALLSIGN.fullmatch(x):
            raise ValueError(f"callsigns: {x[:40]!r} isn't a call sign (letters, digits, spaces; 40 at most)")
        if x not in out:
            out.append(x)
    if len(out) > CALLSIGNS_MAX:
        raise ValueError(f"callsigns: {CALLSIGNS_MAX} at most")
    return out


def context_list(names=(), callsigns=(), planes=(), terms=(), cap: int = CONTEXT_MAX) -> list[str]:
    """A llama-asr model's context: who keyed up, the channel's call signs, the aircraft, then the channel's terms, each
    once (case folded), at most cap."""
    out: list[str] = []
    seen: set = set()
    for group in (names, callsigns, planes, terms):
        for x in group:
            x = " ".join(str(x or "").split()).strip(" ,.")
            if x and x.lower() not in seen and len(x) <= 60:
                seen.add(x.lower())
                out.append(x)
                if len(out) >= cap:
                    return out
    return out


def context_version(form: str, prompt: str, context: list) -> tuple[str, int]:
    """(a short hash, its length in characters) of the context a clip was actually decoded with ("prompt": whisper's
    prompt; "list": the list as sent; else none): what the transcript keeps, never the context itself."""
    text = prompt if form == "prompt" else ", ".join(context) if form == "list" else ""
    return (hashlib.sha1(text.encode()).hexdigest()[:10] if text else "", len(text))


class Choices:
    """Each channel's model, context switch and call signs (hub.sqlite meta META), over DEFAULTS. mdb: the MonitorDb
    (get / put of its meta)."""

    def __init__(self, mdb, catalog: dict | None = None, digest: str = "", clock: Callable[[], float] = time.time):
        self.mdb, self.clock = mdb, clock
        if catalog is None:
            catalog, digest = load()
        self.catalog, self.digest = catalog, digest
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] = (0.0, {})
        self.service_of: Callable[[str], str] = lambda key: ""    # a channel key's service (the hub's: _key_service)

    def settings(self) -> dict:
        """{channel key: {model?, context?, callsigns?}} as stored (read at most once a second: asked per clip)."""
        now = self.clock()
        at, s = self._cache
        if at and now - at < 1.0:
            return s
        try:
            raw = json.loads(self.mdb.get(META, "") or "{}")
        except ValueError:
            raw = {}
        s = {}
        for k, v in (raw.items() if isinstance(raw, dict) else ()):
            if isinstance(k, str) and isinstance(v, dict):
                s[k] = {f: v[f] for f in FIELDS if f in v}
        self._cache = (now, s)
        return s

    def default_for(self, service: str) -> str:
        mid = DEFAULTS.get(service or "", FALLBACK)
        return mid if mid in self.catalog else FALLBACK

    def model_for(self, key: str, service: str = "") -> str:
        """The model a channel's clips go to: its choice, else its service's default."""
        mid = (self.settings().get(key) or {}).get("model")
        return mid if isinstance(mid, str) and mid in self.catalog else self.default_for(service)

    def context_on(self, key: str, model: str) -> bool:
        c = (self.settings().get(key) or {}).get("context")
        if c in ("on", "off"):
            return c == "on"
        e = self.catalog.get(model)
        return True if e is None else bool(e["context_default"])

    def plain(self, key: str) -> list[str]:
        """The catalog's models this channel's context is off for (the job's `plain`: whichever model decodes it)."""
        return [m for m in self.catalog if not self.context_on(key, m)]

    def challenger_for(self, key: str) -> tuple[str, float] | None:
        """(the challenger model, its share in %) a channel's clips are sampled for, None for none."""
        v = self.settings().get(key) or {}
        m = v.get("challenger")
        if not isinstance(m, str) or m not in self.catalog:
            return None
        pct = v.get("pct")
        return m, float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else PCT_DEFAULT

    def promoted(self, key: str) -> dict:
        v = (self.settings().get(key) or {}).get("promoted")
        return dict(v) if isinstance(v, dict) else {}

    def _write(self, cur: dict) -> None:
        self.mdb.put(META, json.dumps(cur, separators=(",", ":"), sort_keys=True))
        self._cache = (0.0, {})

    def promote(self, key: str, service: str | None = None, why: str = "") -> tuple[dict, dict]:
        """The channel's challenger becomes its model; the model it replaces becomes the challenger (at the same share),
        so the two go on being compared: if the new one scores worse over the samples since (sttscore.should_roll_back),
        roll_back() puts the old one back. (old, new). ValueError with no challenger."""
        with self._lock:
            cur = dict(self.settings())
            old = dict(cur.get(key) or {})
            ch = self.challenger_for(key)
            if ch is None:
                raise ValueError("no challenger on this channel")
            was = self.model_for(key, self.service_of(key) if service is None else service)
            new = dict(old, model=ch[0], challenger=was, promoted={"from": was, "to": ch[0], "at": self.clock(),
                                                                   **({"why": why[:80]} if why else {})})
            cur[key] = new
            self._write(cur)
        return old, new

    def roll_back(self, key: str, why: str) -> tuple[dict, dict] | None:
        """A promotion undone: the model it replaced is the channel's again, the challenger cleared (the rolled-back
        model isn't sampled again until an admin says so). None when nothing was promoted."""
        with self._lock:
            cur = dict(self.settings())
            old = dict(cur.get(key) or {})
            p = old.get("promoted") if isinstance(old.get("promoted"), dict) else None
            if not p or p.get("from") not in self.catalog:
                return None
            new = {k: v for k, v in old.items() if k not in ("promoted", "challenger", "pct")}
            new["model"] = p["from"]
            new["rolled_back"] = {"from": p.get("to"), "at": self.clock(), "why": why[:120]}
            cur[key] = new
            self._write(cur)
        return old, new

    def callsigns(self, key: str) -> list[str]:
        v = (self.settings().get(key) or {}).get("callsigns")
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    def want(self, services: dict | None = None) -> list[str]:
        """The models the GPU workers are asked to run, most important first: each service's (fire, EMS, police, air
        ...) default, then the channels' own choices by their service (services: {channel key: service})."""
        services = services or {}
        out: list[str] = []
        for svc in SERVICE_ORDER:
            mids = [self.default_for(svc)] if svc in DEFAULTS else []
            mids += [self.model_for(k, svc) for k, s in sorted(services.items()) if s == svc]
            if svc == "":
                mids += [v.get("model") for k, v in sorted(self.settings().items()) if k not in services]
            for m in mids:
                if isinstance(m, str) and m in self.catalog and m not in out:
                    out.append(m)
        # then the challengers (after every champion: one that doesn't fit the VRAM is the one left out), fire's first
        order = {s: i for i, s in enumerate(SERVICE_ORDER)}
        chall = sorted(((order.get(services.get(k, ""), len(order)), k, v.get("challenger"))
                        for k, v in self.settings().items() if v.get("challenger")), key=lambda x: (x[0], x[1]))
        for _o, _k, m in chall:
            if isinstance(m, str) and m in self.catalog and m not in out:
                out.append(m)
        return out[:WANT_MAX]

    def set(self, key: str, body: dict) -> tuple[dict, dict]:
        """One channel's {model?, context?, callsigns?} (null or "" a field back to its default). (old, new). ValueError."""
        from sizeup import channels
        if not isinstance(key, str) or not channels.KEY.fullmatch(key):
            raise ValueError("key: a channel label or a talkgroup key")
        with self._lock:
            cur = dict(self.settings())
            old = dict(cur.get(key) or {})
            new = dict(old)
            if "model" in body:
                m = body.get("model")
                if m in (None, ""):
                    new.pop("model", None)
                elif not isinstance(m, str) or m not in self.catalog:
                    raise ValueError(f"model: one of {', '.join(self.catalog) or '(no catalog)'}")
                else:
                    new["model"] = m
            if "context" in body:
                c = body.get("context")
                if c in (None, "", "default"):
                    new.pop("context", None)
                elif c not in ("on", "off"):
                    raise ValueError("context: on, off or default")
                else:
                    new["context"] = c
            if "callsigns" in body:
                cs = clean_callsigns(body.get("callsigns"))
                if cs:
                    new["callsigns"] = cs
                else:
                    new.pop("callsigns", None)
            if "challenger" in body:
                c = body.get("challenger")
                if c in (None, ""):
                    for f in ("challenger", "pct"):
                        new.pop(f, None)
                elif not isinstance(c, str) or c not in self.catalog:
                    raise ValueError(f"challenger: one of {', '.join(self.catalog)}")
                else:
                    new["challenger"] = c
            if "pct" in body:
                v = body.get("pct")
                if v in (None, ""):
                    new.pop("pct", None)
                elif isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 100:
                    raise ValueError("pct: a share of the channel's clips, above 0 and at most 100")
                else:
                    new["pct"] = float(v)
            if new.get("challenger") and new.get("challenger") == (new.get("model") or old.get("model")):
                raise ValueError("challenger: not the channel's own model")
            if new:
                cur[key] = new
            else:
                cur.pop(key, None)
            self._write(cur)
        return old, new

    def panel(self) -> dict:
        """Settings: the catalog (id, kind, what, services, context default), the defaults by service and each
        channel's choices."""
        return {"catalog": [dict(e) for e in self.catalog.values()], "defaults": {s: self.default_for(s)
                                                                                for s in DEFAULTS},
                "fallback": FALLBACK, "channels": self.settings()}


WORD = re.compile(r"[a-z0-9']+")


def is_loop(text: str) -> bool:
    """A repetition loop (a model stuck on a phrase: every one here does it on silence or noise): a run of one to eight
    words said again and again in a row (a word 6 times, a short phrase 4, a longer one 3) that is 40% or more of a
    text of 8 words or more. Real traffic repeats ("units responding, units responding") but not like that."""
    w = WORD.findall(str(text or "").lower())[:2000]
    if len(w) < 8:
        return False
    for n in range(1, 9):
        need = 6 if n == 1 else 4 if n <= 3 else 3
        i = 0
        while i + n * need <= len(w):
            g = w[i:i + n]
            k = 1
            while w[i + k * n:i + (k + 1) * n] == g:
                k += 1
            if k >= need and k * n >= 0.4 * len(w):
                return True
            i += max(1, (k - 1) * n) if k > 1 else 1
    return False


def _items(text: str) -> list[str]:
    return [" ".join(WORD.findall(x.lower())) for x in re.split(r"[,.;:!?]+", str(text or "")) if WORD.findall(x.lower())]


def context_echo(text: str, context: list) -> bool:
    """A llama-asr model gave its context back instead of what was said (Qwen3-ASR does when the list nearly matches a
    short clip): two or more of the text's phrases are context items and they are 80% or more of its phrases."""
    if not text or not context:
        return False
    ctx = {" ".join(WORD.findall(str(c).lower())) for c in context}
    items = _items(text)
    hits = sum(1 for i in items if i in ctx)
    return hits >= 2 and hits >= 0.8 * len(items)
