"""Per-channel speech-to-text choices (user, 2026-10-08: "make it configurable, which channel in the
settings gets recorded ... and of which, which gets transcribed, and which transcription to use").

One model for the FM channels (FG1, FG2, South 2) and every P25 talkgroup, keyed "FG1" / "South 2" /
"tg:307":

    record       keep it: the clip is kept and forwarded, and it can be transcribed. Off for an FM
                 channel: the receiver keeps running (squelch, MDC IDs, "talking now"), nothing is
                 kept. For a talkgroup this is the talkgroup's own Record switch (TalkgroupPrefs:
                 calls dropped on arrival), so it isn't stored here.
    transcribe   run speech-to-text on it at all (off: the clip is kept, no text)
    engine       "local": whisper.cpp large-v3 on the station (whisper-server, :8179)
                 "aws":   Amazon Transcribe streaming from the hub (sizeup/transcribe.py); the station
                          skips whisper and forwards the clip with transcript_pending "aws"
                 "both":  whisper on the station and Transcribe on the hub: Transcribe's text is the
                          transcript, whisper's is kept as the alternative to compare

Defaults: [stt] aws_channels (South 2, FG1, FG2, Fire/EMS 15 = tg:307) record + transcribe with "aws";
[stt] record_only (the hub: WCPD Ch 1 = tg:383, LLT Supervisor 1 = tg:3127, #201) recorded, not
transcribed; everything else as before (record as it was, transcribe on, "local"). An edit is a CAD settings
override (section "channels", key as above, value {record?, transcribe?, engine?}: only what differs
from the default), audited (Settings section "radios"), admins only where roles are enforced.

Where the choices are made and applied (docs/cloud.md): the CAD of record edits them (the hub, or
all-in-one); the hub sends them to the station as a `call` job (set_channel_prefs: the whole map
and whether AWS can take audio now) whenever the station's reported copy differs, so the station
applies them even if it was offline when they were changed; the station reports what it applied
(sig) in its state push, and the hub shows "applied on the station" or "waiting". The station works
from its own config's defaults, so the map the hub sends has the hub's record_only defaults folded in
(for_station(): {"transcribe": false} unless Settings says otherwise): a plain choice that any station
applies, one from before record_only included.

An "aws" or "both" channel uses the local model whenever AWS can't take audio (no AWS here or on the
hub, no credentials, the monthly limit reached): effective().
"""

from __future__ import annotations

import hashlib
import json
import re

ENGINES = ("local", "aws", "both")
FIELDS = ("record", "transcribe", "engine")
KEY = re.compile(r"tg:\d{1,9}|[A-Za-z0-9][A-Za-z0-9 ./_-]{0,39}")
DEFAULT_AWS = ("South 2", "FG1", "FG2", "tg:307")
DEFAULT_RECORD_ONLY: tuple[str, ...] = ()
SECTION = "channels"


def tg_key(tg: int) -> str:
    return f"tg:{int(tg)}"


def is_tg(key: str) -> bool:
    return key.startswith("tg:")


def defaults(key: str, aws_channels=DEFAULT_AWS, record_only=DEFAULT_RECORD_ONLY) -> dict:
    return {"record": True, "transcribe": key not in record_only, "engine": "aws" if key in aws_channels else "local"}


def clean(key: str, raw: dict) -> dict:
    """{record?, transcribe?, engine?} as given (checked). ValueError with the reason."""
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ValueError("key: a channel label (FG1) or tg:<talkgroup>")
    if not isinstance(raw, dict):
        raise ValueError("an object")
    out = {}
    for f in ("record", "transcribe"):
        if raw.get(f) is not None:
            if not isinstance(raw[f], bool):
                raise ValueError(f"{f}: true or false")
            out[f] = raw[f]
    if raw.get("engine") is not None:
        if raw["engine"] not in ENGINES:
            raise ValueError(f"engine: one of {', '.join(ENGINES)}")
        out["engine"] = raw["engine"]
    return out


def pref(key: str, overrides: dict, aws_channels=DEFAULT_AWS, record_only=DEFAULT_RECORD_ONLY) -> dict:
    p = defaults(key, aws_channels, record_only)
    o = overrides.get(key)
    if isinstance(o, dict):
        p.update({k: v for k, v in o.items() if k in FIELDS})
    return p


def for_station(overrides: dict, record_only=DEFAULT_RECORD_ONLY) -> dict:
    """The map the hub sends the station (set_channel_prefs): its Settings overrides, plus {"transcribe": false}
    for each record_only channel whose override doesn't say (the station's defaults don't know the hub's
    record_only; a Settings choice wins). Only fields a station keeps (check_map), so its sig agrees."""
    out = {k: dict(v) for k, v in overrides.items() if isinstance(v, dict)}
    for key in record_only:
        if KEY.fullmatch(key) and "transcribe" not in out.get(key, {}):
            out[key] = {**out.get(key, {}), "transcribe": False}
    return out


def effective(p: dict, aws_ok: bool) -> str:
    """off | local | aws | both: what runs for this channel now."""
    if not p.get("transcribe", True):
        return "off"
    eng = p.get("engine", "local")
    return eng if eng == "local" or aws_ok else "local"


def sig(overrides: dict, aws_ok: bool, tones: dict | None = None, reserve=None) -> str:
    """The same choices + the same AWS availability (+ the same named tone pairs, #80; + the channels that keep
    AWS past the monthly limit, #195) -> the same short signature (hub and station). None of those: the
    signature from before them."""
    d = {"prefs": {k: {f: v for f, v in sorted(o.items())} for k, o in sorted(overrides.items())}, "aws": bool(aws_ok)}
    if tones:
        d["tones"] = {k: tones[k] for k in sorted(tones)}
    if reserve and not aws_ok:
        d["reserve"] = sorted(str(k) for k in reserve)
    raw = json.dumps(d, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def phrases_sig(phrases: list[str]) -> str:
    """The vocabulary as sent: whether the list in Settings is the one AWS has."""
    return hashlib.sha1(json.dumps(list(phrases)).encode()).hexdigest()[:12]


def check_map(prefs) -> dict:
    """A whole map from the hub (the set_channel_prefs job): checked, cleaned. ValueError."""
    if not isinstance(prefs, dict) or len(prefs) > 2000:
        raise ValueError("prefs: an object")
    out = {}
    for k, v in prefs.items():
        c = clean(k, v)
        if is_tg(k):
            c.pop("record", None)            # a talkgroup's Record is its own switch
        if c:
            out[k] = c
    return out


def vocabulary_suggestions(units: dict, streets: list[str], top_streets: int = 60) -> list[str]:
    """The seed phrase list: every unit's spoken name (Engine 60, Ladder 31, Tower Ladder 2, 23 98 ...),
    PCFD words, and the busiest street names of Port Chester / Rye Brook as said ("North Main Street")."""
    from sizeup.transcribe import street_spoken
    out: list[str] = ["60 Control", "Port Chester", "Rye Brook", "Fire EMS 15", "Port Chester Fire", "Rye Brook Fire",
                      "Westchester", "Fireground", "Fireground 1", "Fireground 2", "Battalion 15", "Airport Incident",
                      "Command", "Mayday", "Tower Ladder", "Ladder", "Engine", "Rescue", "Marine", "Utility"]
    for code, u in units.items():
        s = (u or {}).get("spoken") if isinstance(u, dict) else None
        if s and s not in out:
            out.append(str(s))
    for name in streets[:top_streets]:
        s = street_spoken(name)
        if s not in out:
            out.append(s)
    return out
