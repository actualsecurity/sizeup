"""Per-channel speech-to-text models on the GPU workers (#639): the catalog, each channel's model and context, the models
the workers are asked to run, and the context each clip carries.

    catalog     deploy/stt-worker/models.json "stt_models" (the worker checks it whole: worker.py check_catalog): each model's
                kind (whisper-ggml: whisper-server, its context a prompt; llama-asr: llama-server with an audio model,
                its context a short list), VRAM, services and context_default. The hub tells the workers which catalog
                (word(): the commit it was built from and models.json's SHA-256) and which models it wants (want()).
    choices     hub.sqlite meta "stt_models" (JSON {channel key: {model?, context?, callsigns?}}): Settings > Radios >
                <channel> > Speech model, admins, audited. Not set: FALLBACK, today's whisper, whatever the service: the
                live path never changes model by itself (no slowdown, no untested model on fire traffic); an admin
                switches a channel once the evidence is there (sizeup/sttscore.py). RECOMMENDED by service (fire, EMS
                and police: Qwen3-ASR; airband: the ATC whisper) is what Settings suggests. context "on" | "off" over the
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
PINS = ROOT / "deploy" / "stt-worker" / "models.json"
META = "stt_models"
KINDS = ("whisper-ggml", "llama-asr")
ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
FALLBACK = "large-v3-turbo-q5_0"              # today's whisper (every worker's whisper_server)
RECOMMENDED = {"fire": "qwen3-asr-1.7b-q8_0", "ems": "qwen3-asr-1.7b-q8_0", "police": "qwen3-asr-1.7b-q8_0",
               "air": "large-v3-turbo-us-atc-v3-f16"}    # (suggested in Settings, never applied by itself)
DEFAULTS: dict = {}                                    # a service's own default model: none (FALLBACK for all)
SERVICE_ORDER = ("fire", "ems", "police", "air", "transit", "other", "")   # whose model is loaded first (VRAM)
CONTEXT_MAX = 12
CALLSIGNS_MAX = 8
CALLSIGN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .'/-]{0,39}")
WANT_MAX = 8
FIELDS = ("model", "context", "callsigns", "challenger", "pct", "promoted", "rolled_back", "shadow")
PCT_DEFAULT = 10.0           # a challenger's share of its channel's clips
# the GPU worker's local LLM (the intel lane's, sizeup/cloud/infer.py): models.json "llm_models" (the worker checks
# it, downloads the GGUF and swaps llama-server; README "The local LLM"). The hub names one (Settings > Station >
# Health, beside the LLM's status); DEFAULT_LLM is today's 8B: the 4B only once an admin picks it (intel's cue gate first)
LLM_META = "stt_llm"
DEFAULT_LLM = "qwen3-8b-q4_k_m"
LLM_PRIORITIES = ("yield", "keep")      # "yield": speech first, the LLM gives its VRAM to the speech models (the admin's)
DEFAULT_LLM_PRIORITY = "yield"
# a champion change on a pre-alert channel (#728: intel is told before it applies): kept in hub.sqlite meta, newest last
CHANGES_META = "stt_champion_log"
CHANGES_MAX = 200


def load(path: Path | str = PINS) -> tuple[dict, str]:
    """(the catalog {id: {kind, vram_mb, services, context, context_default, what}}, models.json's SHA-256); ({}, "") when
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


def load_llms(path: Path | str = PINS) -> dict:
    """models.json's llm_models: {id: {id, what, vram_mb}} (what the hub shows and names; the worker checks the files).
    {} when there are none (a catalog from before them: the workers keep worker.toml's)."""
    try:
        doc = json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return {}
    out = {}
    for mid, e in (doc.get("llm_models") or {}).items() if isinstance(doc, dict) else ():
        if mid.startswith("_") or not ID.fullmatch(mid) or not isinstance(e, dict):
            continue
        out[mid] = {"id": mid, "what": str(e.get("what") or "")[:120], "vram_mb": int(e.get("vram_mb") or 0)}
    return out


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

    def __init__(self, mdb, catalog: dict | None = None, digest: str = "", clock: Callable[[], float] = time.time,
                 llms: dict | None = None):
        self.mdb, self.clock = mdb, clock
        if catalog is None:
            catalog, digest = load()
        self.catalog, self.digest = catalog, digest
        self.llms = load_llms() if llms is None else llms
        self._lock = threading.RLock()        # (set() calls its before hook holding it: note_change takes it again)
        self._cache: tuple[float, dict] = (0.0, {})
        self.service_of: Callable[[str], str] = lambda key: ""    # a channel key's service (the hub's: _key_service)

    def settings(self) -> dict:
        """{channel key: {model?, context?, callsigns?, ...}} as stored (read at most once a second: asked per clip)."""
        now = self.clock()
        at, s = self._cache
        if at and now - at < 1.0:
            return s
        s = self._read()
        self._cache = (now, s)
        return s

    def _read(self) -> dict:
        """The choices as the store has them now (the writers' read, under the lock: never the cache)."""
        try:
            raw = json.loads(self.mdb.get(META, "") or "{}")
        except ValueError:
            raw = {}
        s = {}
        for k, v in (raw.items() if isinstance(raw, dict) else ()):
            if isinstance(k, str) and isinstance(v, dict):
                s[k] = {f: v[f] for f in FIELDS if f in v}
        return s

    def default_for(self, service: str) -> str:
        mid = DEFAULTS.get(service or "", FALLBACK)
        return mid if mid in self.catalog else FALLBACK

    def model_for(self, key: str, service: str = "") -> str:
        """The model a channel's clips go to: its choice, else its service's default."""
        mid = (self.settings().get(key) or {}).get("model")
        return mid if isinstance(mid, str) and mid in self.catalog else self.default_for(service)

    # an airband channel (the hub sets it: its kind): a whisper model there never gets a prompt (#900, HubApp's
    # _pool_prompt_v: an ATC prompt costs words and makes it loop on a weak AM clip), whatever its context switch; the
    # aircraft near the field go only to a model that takes a list
    is_airband: Callable[[str], bool] = staticmethod(lambda key: False)

    def context_on(self, key: str, model: str) -> bool:
        e = self.catalog.get(model)
        if e is not None and e["context"] == "prompt" and self.is_airband(key):
            return False
        c = (self.settings().get(key) or {}).get("context")
        if c in ("on", "off"):
            return c == "on"
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

    def shadow_on(self, key: str) -> bool:
        """An AWS channel's GPU candidates decode a share of its clips in shadow against AWS (#878): on unless an
        admin turned it off."""
        return (self.settings().get(key) or {}).get("shadow") != "off"

    def candidates(self, key: str, service: str = "") -> list[str]:
        """An AWS channel's GPU candidates (#878): the model it would run on a GPU (its choice, else today's), and its
        challenger when it has one."""
        out = [self.model_for(key, service)]
        c = self.challenger_for(key)
        if c is not None and c[0] not in out:
            out.append(c[0])
        return out

    def promoted(self, key: str) -> dict:
        v = (self.settings().get(key) or {}).get("promoted")
        return dict(v) if isinstance(v, dict) else {}

    def _write(self, cur: dict) -> None:
        self.mdb.put(META, json.dumps(cur, separators=(",", ":"), sort_keys=True))
        self._cache = (0.0, {})

    def promote(self, key: str, service: str | None = None, why: str = "", expect: tuple | None = None,
                before: Callable[[str, str, str], None] | None = None) -> tuple[dict, dict]:
        """The channel's challenger becomes its model; the model it replaces becomes the challenger (at the same share),
        so the two go on being compared: if the new one scores worse over the samples since (sttscore.should_roll_back),
        roll_back() puts the old one back. (old, new). ValueError with no challenger, or when the pair isn't expect
        (champion, challenger: the one the evidence was for). before(key, from, to): under the lock, before it is
        stored (intel told first: HubApp.stt_champion_change)."""
        with self._lock:
            cur = dict(self._read())
            key = self.key_of(key)
            old = dict(cur.get(key) or {})
            m = old.get("challenger")
            ch = (m, float(old.get("pct") or PCT_DEFAULT)) if isinstance(m, str) and m in self.catalog else None
            if ch is None:
                raise ValueError("no challenger on this channel")
            was = old.get("model") if old.get("model") in self.catalog else \
                self.default_for(self.service_of(key) if service is None else service)
            if expect is not None and (was, ch[0]) != tuple(expect):
                raise ValueError("the channel's models changed meanwhile: look at the evidence again")
            if before is not None:
                before(key, was, ch[0])
            new = dict(old, model=ch[0], challenger=was, promoted={"from": was, "to": ch[0], "at": self.clock(),
                                                                   **({"why": why[:80]} if why else {})})
            cur[key] = new
            self._write(cur)
        return old, new

    def roll_back(self, key: str, why: str, expect_to: str | None = None,
                  before: Callable[[str, str, str], None] | None = None) -> tuple[dict, dict] | None:
        """A promotion undone: the model it replaced is the channel's again, the challenger cleared (the rolled-back
        model isn't sampled again until an admin says so). None when nothing was promoted."""
        with self._lock:
            cur = dict(self._read())
            old = dict(cur.get(key) or {})
            p = old.get("promoted") if isinstance(old.get("promoted"), dict) else None
            if not p or p.get("from") not in self.catalog or (expect_to is not None and p.get("to") != expect_to):
                return None
            if before is not None:
                before(key, str(p.get("to") or ""), str(p["from"]))
            new = {k: v for k, v in old.items() if k not in ("promoted", "challenger", "pct")}
            new["model"] = p["from"]
            new["rolled_back"] = {"from": p.get("to"), "at": self.clock(), "why": why[:120]}
            cur[key] = new
            self._write(cur)
        return old, new

    def callsigns(self, key: str) -> list[str]:
        v = (self.settings().get(key) or {}).get("callsigns")
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    def want(self, services: dict | None = None, off_gpu=()) -> list[str]:
        """The models the GPU workers are asked to run, most important first: each service's (fire, EMS, police, air
        ...) default, then the channels' own choices by their service (services: {channel key: service}). off_gpu: the
        channels whose engine never decodes on a GPU live (pinned AWS, Local, Compare): their model isn't resident."""
        services = {k: v for k, v in (services or {}).items() if k not in off_gpu}
        out: list[str] = []
        for svc in SERVICE_ORDER:
            mids = [self.default_for(svc)] if svc in DEFAULTS else []
            mids += [self.model_for(k, svc) for k, s in sorted(services.items()) if s == svc]
            if svc == "":
                mids += [v.get("model") for k, v in sorted(self.settings().items())
                         if k not in services and k not in off_gpu]
            for m in mids:
                if isinstance(m, str) and m in self.catalog and m not in out:
                    out.append(m)
        return out[:WANT_MAX]

    def tries(self, services: dict | None = None, want: list | None = None) -> list[str]:
        """The challengers (#869): the models a worker loads only on demand, never in its resident plan (want): only while
        idle, only beside the champions AND the LLM, unloaded under pressure (the worker's stt_models.try). Fire's
        first; none that is a champion already."""
        services = services or {}
        want = self.want(services) if want is None else want
        order = {s: i for i, s in enumerate(SERVICE_ORDER)}
        chall = sorted(((order.get(services.get(k, ""), len(order)), k, v.get("challenger"))
                        for k, v in self.settings().items() if v.get("challenger")), key=lambda x: (x[0], x[1]))
        out: list[str] = []
        for _o, _k, m in chall:
            if isinstance(m, str) and m in self.catalog and m not in out and m not in want:
                out.append(m)
        return out[:WANT_MAX]

    @staticmethod
    def key_of(key) -> str:
        """A channel key as kept: a label or a talkgroup key; the county's talkgroups as "tg:307", as routed.
        ValueError."""
        from sizeup import channels
        from sizeup import sourcekeys
        if not isinstance(key, str) or not channels.KEY.fullmatch(key):
            raise ValueError("key: a channel label or a talkgroup key")
        got = sourcekeys.parse(key) if key.startswith("p25:") else None
        if got is not None and got[0] == sourcekeys.COUNTY:
            key = channels.tg_key(got[1])
        return key

    def set(self, key: str, body: dict, before: Callable[[str, dict, dict], None] | None = None) -> tuple[dict, dict]:
        """One channel's {model?, context?, callsigns?, challenger?, pct?} (null or "" a field back to its default).
        (old, new). ValueError. before(key, old, new): called once every field is checked, before it is stored (a
        champion change: intel told first, HubApp.stt_champion_change)."""
        key = self.key_of(key)
        with self._lock:
            cur = dict(self._read())
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
            if "shadow" in body:
                v = body.get("shadow")
                if v in (None, "", "on", "default"):
                    new.pop("shadow", None)
                elif v != "off":
                    raise ValueError("shadow: on or off")
                else:
                    new["shadow"] = "off"
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
            if new.get("model") != old.get("model") or new.get("challenger") != old.get("challenger"):
                new.pop("promoted", None)            # (a promotion is watched only while its pair stands)
            if before is not None and old != new:
                before(key, old, new)
            if new:
                cur[key] = new
            else:
                cur.pop(key, None)
            self._write(cur)
        return old, new

    # -- the workers' local LLM: which model, and who gives way on VRAM ---------------------------------------------

    def _llm_doc(self) -> dict:
        try:
            d = json.loads(self.mdb.get(LLM_META, "") or "{}")
        except ValueError:
            d = {}
        return d if isinstance(d, dict) else {}

    def llm(self) -> str:
        """The LLM the workers are told to run (an llm_models id): the admin's, else DEFAULT_LLM; "" with no catalog
        entry for it (the workers keep worker.toml's)."""
        m = self._llm_doc().get("model")
        m = m if isinstance(m, str) and m in self.llms else DEFAULT_LLM
        return m if m in self.llms else ""

    def llm_priority(self) -> str:
        p = self._llm_doc().get("priority")
        return p if p in LLM_PRIORITIES else DEFAULT_LLM_PRIORITY

    def set_llm(self, model=None, priority=None) -> tuple[dict, dict]:
        """The admin's LLM (an llm_models id; None or "" back to the default) and/or its priority. (old, new) as
        {model, priority}. ValueError."""
        with self._lock:
            old = {"model": self.llm(), "priority": self.llm_priority()}
            d = self._llm_doc()
            if model is not None:
                if model == "":
                    d.pop("model", None)
                elif not isinstance(model, str) or model not in self.llms:
                    raise ValueError(f"model: one of {', '.join(self.llms) or '(no LLM catalog)'}")
                else:
                    d["model"] = model
            if priority is not None:
                if priority not in LLM_PRIORITIES:
                    raise ValueError(f"priority: one of {', '.join(LLM_PRIORITIES)}")
                d["priority"] = priority
            self.mdb.put(LLM_META, json.dumps(d, separators=(",", ":"), sort_keys=True))
            return old, {"model": self.llm(), "priority": self.llm_priority()}

    def llm_panel(self) -> dict:
        return {"model": self.llm(), "default": DEFAULT_LLM if DEFAULT_LLM in self.llms else "",
                "priority": self.llm_priority(), "priorities": list(LLM_PRIORITIES),
                "catalog": [dict(e) for e in self.llms.values()]}

    # -- champion changes (intel is told first on a pre-alert channel) ------------------------------------------------

    def changes(self, since: float = 0.0) -> list[dict]:
        """The champion changes kept (newest last), after `since`: what the intel session reads (HubApp.stt_champion_log)."""
        try:
            rows = json.loads(self.mdb.get(CHANGES_META, "") or "[]")
        except ValueError:
            rows = []
        return [r for r in rows if isinstance(r, dict) and float(r.get("at") or 0) > since] if isinstance(rows, list) else []

    def note_change(self, row: dict) -> None:
        with self._lock:
            rows = self.changes()
            rows.append(row)
            self.mdb.put(CHANGES_META, json.dumps(rows[-CHANGES_MAX:], separators=(",", ":"), sort_keys=True))

    def panel(self) -> dict:
        """Settings: the catalog (id, kind, what, services, context default), the defaults by service and each
        channel's choices."""
        return {"catalog": [dict(e) for e in self.catalog.values()], "defaults": {s: self.default_for(s)
                                                                                for s in DEFAULTS},
                "recommended": {s: m for s, m in RECOMMENDED.items() if m in self.catalog},
                "fallback": FALLBACK, "channels": self.settings()}


WORD = re.compile(r"[a-z0-9']+")


def _items(text: str) -> list[str]:
    return [" ".join(WORD.findall(x.lower())) for x in re.split(r"[,.;:!?]+", str(text or "")) if WORD.findall(x.lower())]


ECHO_MIN_ITEMS = 3        # a read-back: at least this many context phrases ...
ECHO_SLOW_WPS = 0.3       # ... or (two, all of it) words this sparse for a clip ECHO_SLOW_MIN_S or longer: a long clip
ECHO_SLOW_MIN_S = 8.0     # read as the list (a short call-up with a squelch tail and pauses is speech: "908, HQ" in 3 s)


def context_echo(text: str, context: list, seconds: float | None = None) -> bool:
    """A llama-asr model gave its context back instead of what was said (Qwen3-ASR does when the list nearly matches a
    short clip). Every phrase of the text a context item, and either ECHO_MIN_ITEMS or more of them ("Engine 60, Ladder
    31, 60 Control"), or two that are far too few words for a long clip (seconds: ECHO_SLOW_MIN_S or more, under
    ECHO_SLOW_WPS words a second). A real call-up of two names ("Sixty Control, Engine Sixty-Three" said in 2 s) is kept; a phrase that
    isn't a name ("responding", "on scene") is speech."""
    if not text or not context:
        return False
    ctx = {" ".join(WORD.findall(str(c).lower())) for c in context}
    items = _items(text)
    hits = sum(1 for i in items if i in ctx)
    if not items or hits < len(items):
        return False
    if hits >= ECHO_MIN_ITEMS:
        return True
    words = sum(len(i.split()) for i in items)
    return (hits >= 2 and isinstance(seconds, (int, float)) and seconds >= ECHO_SLOW_MIN_S
            and words / seconds < ECHO_SLOW_WPS)
