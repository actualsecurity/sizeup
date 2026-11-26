"""The station's status as the hub takes it (POST /api/station/state "status", StationLink.status): each key the hub
reads, of the type it reads it as, and the talkers / pages going out beside it (live_clean). Checked before anything is
stored (Hub._state): a status of another shape is left out (the last good one stays) and a talker / page of another
shape dropped, the rest of the post taken and the answer saying so ("refused"), so a wrong-typed field can never take a
route down that reads it later (GET /api/cad/state, /api/state, /api/station/status, Health), and a station of any
release still hears the hub's build. contracts/schemas/station-state.schema.json (#774) is
the full description; this is the part the hub depends on, by hand (jsonschema is a dev dependency only).

Lenient where the hub is: a key that is absent is fine (an older station has fewer), unknown keys pass (a newer one has
more), and only the keys the hub's readers dereference are typed. problem(status) -> None, or the first problem as
"status.<path>: <what it must be>" (fixed text: never the value itself).
"""

from __future__ import annotations

import math
from typing import Any, Callable


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str(v) -> bool:
    return isinstance(v, str)


def _dict(v) -> bool:
    return isinstance(v, dict)


def _list(v) -> bool:
    return isinstance(v, list)


def _opt(f: Callable[[Any], bool]) -> Callable[[Any], bool]:
    return lambda v: v is None or f(v)


# (check, what it must be): for a key that is there
STR = (_str, "a string")
OSTR = (_opt(_str), "a string or null")
NUM = (_num, "a number")
ONUM = (_opt(_num), "a number or null")
INT = (_int, "an integer")
DICT = (_dict, "an object")
ODICT = (_opt(_dict), "an object or null")
LIST = (_list, "a list")
OLIST = (_opt(_list), "a list or null")

RADIO_ROW = {"label": STR, "state": OSTR, "detail": OSTR, "word": OSTR, "mhz": ONUM, "last_heard": ONUM,
             "open_s": ONUM, "squelch_default": ODICT, "system": OSTR}
SDR = {"sdrs": OLIST, "groups": OLIST, "duplicates": OLIST}
RERUN = {"total": NUM, "done": NUM, "changed": NUM, "failed": NUM, "pending": NUM}
CHANNELS = {"prefs": DICT, "aws": DICT, "squelch": DICT, "sig": STR}
RECEPTION = {"history": LIST}
STT_TERMS = {"v": OSTR, "sets": DICT}
OUTBOX = {"pending": (_opt(lambda v: _int(v) and v >= 0), "a count or null"), "oldest": ONUM,
          "dropped_audio": (_opt(lambda v: _int(v) and v >= 0), "a count or null"),
          "quarantine": ODICT}     # (the events the hub refused: health.py _quarantine checks each field it reads)
STT_INPUT = {"dropped": DICT, "worker": DICT}
VERSION = {"build": OSTR, "hub": OSTR, "update": DICT, "adapter": ODICT, "state": OSTR, "text": OSTR}
# the updater's state as the hub reads it (autoupdate.describe: on the deploy check's path)
VERSION_UPDATE = {"state": OSTR, "phase": OSTR, "to": OSTR, "from": OSTR, "since": ONUM, "failed": ODICT, "equivalent": ODICT,
                  "retry": ODICT, "hold": ODICT, "last": ODICT}
# whisper's queue as Health's watcher reads it (the backlog alert, its text and cause)
STT_QUEUE = {**{k: ONUM for k in ("queued", "urgent", "oldest_s", "service_s", "in_h", "in_fallback_h")},
             "top": OLIST, "in_top": OLIST}
HEALTH = {k: ODICT for k in ("jobs", "recorder", "whisper", "stt_queue", "reception", "usb", "heard", "adsb",
                             "link_restarts")}
TALKGROUP = {"tg": INT}

TOP: dict[str, tuple] = {
    "id": STR, "config_id": OSTR, "name": OSTR,
    "radios": LIST, "sdr": ODICT, "rerun": DICT, "channels": DICT, "p25_systems": LIST, "reception": ODICT,
    "stt_terms": DICT, "outbox": DICT, "stt_input": DICT, "pool_worker": DICT, "stt_alert_rev": INT, "caps": LIST,
    "version": DICT, "logship": DICT, "metrics": DICT, "health": DICT, "talkgroups": LIST,
}
NESTED: dict[str, dict] = {"sdr": SDR, "rerun": RERUN, "channels": CHANNELS, "reception": RECEPTION,
                           "stt_terms": STT_TERMS, "outbox": OUTBOX, "stt_input": STT_INPUT, "version": VERSION,
                           "health": HEALTH}
DEEP: dict[tuple[str, str], dict] = {("version", "update"): VERSION_UPDATE, ("health", "stt_queue"): STT_QUEUE}
ROWS: dict[str, dict] = {"radios": RADIO_ROW, "p25_systems": {}, "talkgroups": TALKGROUP}   # lists of objects


OBOOL = (_opt(lambda v: isinstance(v, bool)), "true, false or null")
OINT = (_opt(_int), "an integer or null")
# "talking" and "paging", beside the status (RemoteLive on the hub: each entry until its `until`): only what the hub and
# the consoles compute with (the rest is shown as text)
TALKER = {"system": OSTR, "channel": OSTR, "tg": OINT, "radio_id": OSTR, "since": ONUM, "talk_since": ONUM,
          "ended": OBOOL, "until": ONUM}
PAGE = {"channel": OSTR, "started": ONUM, "ended": OBOOL, "until": ONUM}
PAIR = {"a": ONUM, "b": ONUM, "names": OLIST}


def _fields(d: dict, spec: dict, path: str) -> str | None:
    for k, (ok, what) in spec.items():
        if k in d and not ok(d[k]):
            return f"{path}.{k}: {what}"
    return None


def problem(status) -> str | None:
    """None when the hub can take this status; else why not (the first problem found)."""
    if not isinstance(status, dict):
        return "status: an object"
    bad = _fields(status, TOP, "status")
    if bad:
        return bad
    for k, spec in NESTED.items():
        v = status.get(k)
        if isinstance(v, dict) and (bad := _fields(v, spec, f"status.{k}")):
            return bad
    for (k, sub), spec in DEEP.items():
        v = (status.get(k) or {}).get(sub)
        if isinstance(v, dict) and (bad := _fields(v, spec, f"status.{k}.{sub}")):
            return bad
    for k, spec in ROWS.items():
        for i, r in enumerate(status.get(k) or []):
            if not isinstance(r, dict):
                return f"status.{k}[{i}]: an object"
            if bad := _fields(r, spec, f"status.{k}[{i}]"):
                return bad
    jobs = (status.get("health") or {}).get("jobs") or {}
    for j, v in jobs.items():
        if not isinstance(v, dict):
            return "status.health.jobs.*: objects"
    return None


def _live_entry(name: str, i: int, e) -> str | None:
    if not isinstance(e, dict) or (name == "paging" and not isinstance(e.get("pairs"), list)):
        return None                                 # (RemoteLive drops it as it always did)
    if bad := _fields(e, TALKER if name == "talking" else PAGE, f"{name}[{i}]"):
        return bad
    for j, pr in enumerate(e["pairs"] if name == "paging" else []):
        if not isinstance(pr, dict):
            return f"{name}[{i}].pairs[{j}]: an object"
        if bad := _fields(pr, PAIR, f"{name}[{i}].pairs[{j}]"):
            return bad
    return None


def live_clean(talking, paging) -> tuple:
    """(talking, paging, why): a state post's talkers and pages going out without the entries the hub can't read (a
    field of another type than it computes with, e.g. a word for `until`), and why the first was left out (None: none
    was). The rest is as it came: what RemoteLive already drops (a list that is not one, an entry that is not an
    object, a page without a list of pairs) it still drops."""
    why = None
    out = []
    for name, entries in (("talking", talking), ("paging", paging)):
        if not isinstance(entries, list):
            out.append(entries)
            continue
        kept = []
        for i, e in enumerate(entries):
            bad = _live_entry(name, i, e)
            if bad:
                why = why or bad
            else:
                kept.append(e)
        out.append(kept)
    return out[0], out[1], why
