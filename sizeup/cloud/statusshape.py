"""The station's status as the hub takes it (POST /api/station/state "status", StationLink.status): each key the hub
reads, of the type it reads it as. Checked before the status is stored (Hub._state): a status of another shape is
refused with a 400 and the last good one stays, so a wrong-typed field can never take a route down that reads it later
(GET /api/cad/state, /api/state, /api/station/status, Health). contracts/schemas/station-state.schema.json (#774) is
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
          "dropped_audio": (_opt(lambda v: _int(v) and v >= 0), "a count or null")}
STT_INPUT = {"dropped": DICT, "worker": DICT}
VERSION = {"build": OSTR, "hub": OSTR, "update": DICT, "adapter": ODICT, "state": OSTR, "text": OSTR}
HEALTH = {k: ODICT for k in ("jobs", "recorder", "whisper", "stt_queue", "reception", "usb", "heard", "adsb",
                             "link_restarts")}
TALKGROUP = {"tg": INT}

TOP: dict[str, tuple] = {
    "id": STR, "config_id": OSTR, "name": OSTR,
    "radios": LIST, "sdr": ODICT, "rerun": DICT, "channels": DICT, "p25_systems": LIST, "reception": ODICT,
    "stt_terms": DICT, "outbox": DICT, "stt_input": DICT, "pool_worker": DICT, "stt_alert_rev": INT,
    "version": DICT, "logship": DICT, "metrics": DICT, "health": DICT, "talkgroups": LIST,
}
NESTED: dict[str, dict] = {"sdr": SDR, "rerun": RERUN, "channels": CHANNELS, "reception": RECEPTION,
                           "stt_terms": STT_TERMS, "outbox": OUTBOX, "stt_input": STT_INPUT, "version": VERSION,
                           "health": HEALTH}
ROWS: dict[str, dict] = {"radios": RADIO_ROW, "p25_systems": {}, "talkgroups": TALKGROUP}   # lists of objects


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
