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

Squelch (Settings > Radios > Squelch): an FM channel's noise squelch, open_db / close_db (quieting under the idle
hiss) and min_open_s (carrier shorter than this: nothing kept, never "talking"; sizeup/radio/fm.py). Its own
section ("squelch", key the channel's label, value only what differs from the receiver's default), audited, and
sent to the station with the channel choices (set_channel_prefs' squelch, in the same signature), applied live.

An "aws" or "both" channel uses the local model whenever AWS can't take audio (no AWS here or on the
hub, no credentials, the monthly limit reached): effective(). Another P25 system's talkgroup ("p25:mrrs:9211", police
dispatch: AWS by default, radio-intel PR 3) has its own monthly budget: the hub's word on AWS lists it under "reserve"
when only ours is used up, and under "deny" when only its own is (the station's whisper takes it).
"""

from __future__ import annotations

import hashlib
import json
import re

ENGINES = ("local", "aws", "both")
FIELDS = ("record", "transcribe", "engine")
# "tg:307": the county system's talkgroup; "p25:<system>:<tg>": another P25 system's (#409, sizeup/sourcekeys.py)
KEY = re.compile(r"tg:\d{1,9}|p25:[a-z][a-z0-9_-]{0,23}:\d{1,9}|[A-Za-z0-9][A-Za-z0-9 ./_-]{0,39}")
DEFAULT_AWS = ("South 2", "FG1", "FG2", "tg:307")
DEFAULT_RECORD_ONLY: tuple[str, ...] = ()
SECTION = "channels"
SQUELCH_SECTION = "squelch"
# field -> (low, high, step): what Settings takes. Defaults: the receiver's (radio/fm.py OPEN_DB, CLOSE_DB, MIN_OPEN_S)
SQUELCH = {"open_db": (4.0, 30.0, 0.5), "close_db": (2.0, 28.0, 0.5), "min_open_s": (0.1, 2.0, 0.05)}
CHATTER_PER_MIN = 2.0      # short opens (nothing kept) per minute, over 5 min: the squelch chattering on noise (a hint)


def tg_key(tg: int) -> str:
    return f"tg:{int(tg)}"


def is_tg(key: str) -> bool:
    return key.startswith(("tg:", "p25:"))


def tg_of(key: str) -> tuple[str, int]:
    """(system, talkgroup) of a talkgroup's key: system "" for the county's ("tg:307")."""
    from sizeup import sourcekeys as sources
    got = sources.parse(key)
    if got is None:
        raise ValueError(f"not a talkgroup key: {key!r}")
    return ("" if key.startswith("tg:") else got[0]), got[1]


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


def sig(overrides: dict, aws_ok: bool, tones: dict | None = None, reserve=None, squelch: dict | None = None,
        deny=None) -> str:
    """The same choices + the same AWS availability (+ the same named tone pairs, #80; + the channels that keep
    AWS past the monthly limit, #195; + the squelch settings; + the context channels past their own AWS budget while
    ours isn't, radio-intel PR 3) -> the same short signature (hub and station). None of those: the signature from
    before them."""
    d = {"prefs": {k: {f: v for f, v in sorted(o.items())} for k, o in sorted(overrides.items())}, "aws": bool(aws_ok)}
    if squelch:
        d["squelch"] = {k: {f: v for f, v in sorted(o.items())} for k, o in sorted(squelch.items())}
    if tones:
        d["tones"] = {k: tones[k] for k in sorted(tones)}
    if reserve and not aws_ok:
        d["reserve"] = sorted(str(k) for k in reserve)
    if deny:
        d["deny"] = sorted(str(k) for k in deny)
    raw = json.dumps(d, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def clean_squelch(key: str, raw: dict) -> dict:
    """{open_db?, close_db?, min_open_s?} for an FM channel (checked, rounded to its step). ValueError."""
    if not isinstance(key, str) or not KEY.fullmatch(key) or is_tg(key):
        raise ValueError("key: an FM channel's label (FG1)")
    if not isinstance(raw, dict):
        raise ValueError("an object")
    out = {}
    for f, (lo, hi, step) in SQUELCH.items():
        v = raw.get(f)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= float(v) <= hi:
            raise ValueError(f"{f}: {lo:g} to {hi:g}")
        out[f] = round(round(float(v) / step) * step, 2)
    if "open_db" in out and "close_db" in out and out["close_db"] >= out["open_db"]:
        raise ValueError("close_db: under open_db")
    return out


def norm_squelch(m) -> dict:
    """A stored squelch map as the hub sends it and the station keeps it (the same on both, so their signatures
    agree): each entry cleaned, empty or invalid ones dropped."""
    out = {}
    for k, v in (m.items() if isinstance(m, dict) else ()):
        try:
            c = clean_squelch(k, v)
        except ValueError:
            continue
        if c:
            out[k] = c
    return out


def fit_squelch(m, kinds: dict, defaults) -> tuple[dict, list[str]]:
    """What of a squelch map applies to a station: its traffic channels only (a channel renamed, gone or now paging:
    left out, never an error that would stop the rest of the channel push), close under open with the station's
    own defaults (defaults(key) -> {open_db, close_db, min_open_s}). The hub (against what the station reported)
    and the station (against its own) leave out the same entries, so their signatures agree. -> (kept, why not)."""
    kept, why = {}, []
    for k, v in norm_squelch(m).items():
        if kinds.get(k) != "traffic":
            why.append(f"{k}: {kinds.get(k) or 'no such'} channel")
            continue
        eff = {**(defaults(k) or {}), **v}
        if eff.get("open_db") is not None and eff.get("close_db") is not None and eff["close_db"] >= eff["open_db"]:
            why.append(f"{k}: close {eff['close_db']:g} dB not under open {eff['open_db']:g} dB")
            continue
        kept[k] = v
    return kept, why


def check_squelch(m) -> dict:
    """A whole squelch map from the hub: checked, cleaned. ValueError."""
    if not isinstance(m, dict) or len(m) > 50:
        raise ValueError("squelch: an object")
    return {k: c for k, v in m.items() if (c := clean_squelch(k, v))}


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


# the unit status words (sizeup/cad/statushints.py PHRASES), as said on the air
STATUS_WORDS = ("en route", "responding", "on location", "on scene", "arrived", "in service", "back in service",
                "available", "returning", "in quarters", "out of service", "staging", "in the area")
# a few of them in radio phrasing, with a unit of each kind: the phrase as a whole is what AWS hears ("Engine 61 en route")
STATUS_SAID_BY = {"E61": "en route", "E62": "on location", "L31": "responding", "R41": "en route", "2391": "on location",
                  "2394": "en route", "TL2": "in service", "E64": "in the area"}


def vocabulary_suggestions(units: dict, streets: list[str], top_streets: int = 60, county=()) -> list[str]:
    """The seed phrase list: every unit's spoken name (Engine 60, Ladder 31, Tower Ladder 2, 23 98 ...),
    PCFD words, and the busiest street names of Port Chester / Rye Brook as said ("North Main Street"); then
    county: the county-wide phrases (sizeup/stt_vocab.py county_phrases: callsigns, departments, channels)."""
    from sizeup.transcribe import street_spoken
    out: list[str] = ["60 Control", "Port Chester", "Rye Brook", "Fire EMS 15", "Port Chester Fire", "Rye Brook Fire",
                      "Westchester", "Fireground", "Fireground 1", "Fireground 2", "Battalion 15", "Airport Incident",
                      "Command", "Mayday", "Tower Ladder", "Ladder", "Engine", "Rescue", "Marine", "Utility",
                      # the airport's alerts as 60 Control declares them (#369, sizeup/airport_watch.py)
                      "alert level one", "alert level two", "alert level three", "Westchester County Airport", "HPN",
                      "flaps", "flap issues", "landing gear", "runway one six", "runway three four", "mile final",
                      "landed without incident",
                      # #365: Fire/EMS 15 heard "carbon dioxide"; C.O. is AWS's spelling of an acronym said letter by letter
                      "carbon monoxide", "carbon monoxide detector activation", "C.O. detector", "C.O. alarm",
                      # a unit's status as it says it (2026-11-10: clear "en route" came back "is fine", "around";
                      # "on location" as "on vacation"): the words alone here, in radio phrasing below
                      *STATUS_WORDS]
    for code, u in units.items():
        s = (u or {}).get("spoken") if isinstance(u, dict) else None
        if s and s not in out:
            out.append(str(s))
    for code, said in STATUS_SAID_BY.items():          # "Engine 61 en route", "23 91 on location" (a board's own units)
        s = (units.get(code) or {}).get("spoken") if isinstance(units.get(code), dict) else None
        if s and f"{s} {said}" not in out:
            out.append(f"{s} {said}")
    for name in streets[:top_streets]:
        s = street_spoken(name)
        if s not in out:
            out.append(s)
    return list(dict.fromkeys([*out, *county]))
