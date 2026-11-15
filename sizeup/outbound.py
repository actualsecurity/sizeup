"""Outbound off unless this is production (the guard against a hub on a restored copy reaching the real world).

A hub started by hand on a restored production copy (docs/development.md "Maintainers only") holds the real push topics
in its Settings and may have the real AWS credentials and bucket names in its environment. One forgotten step and it
pushes to the crew's phones, uploads into the backup bucket or pays for Transcribe. So the app decides once, at its
start (sizeup/app.py main: arm), whether it may reach the project's outside services at all:

    on    the hub under its supervisor (sizeup/handoff.py gives the app SIZEUP_LEASE_FILE: the fenced production hub on
          ECS), a station (role "station": its own calls, the hub link included), or SIZEUP_ALLOW_OUTBOUND=1 (a
          person's explicit choice, e.g. an all-in-one with its own ntfy topic)
    off   anything else started with `python -m sizeup`: all-in-one, or a hub without a lease

Off, every outbound path refuses before a byte leaves (check(): its own error, the reason in it): ntfy publishes
(sizeup/notify.py publish: alerts, status, intel, the replica watch's and the error watch's pushes), every AWS request
(sizeup/aws.py sign / presign / json_call: S3 audio, attachments, documents, map caches, CloudWatch Logs, SSM,
Transcribe and its streaming, the Listen links), Polly (sizeup/polly.py) and the Bedrock stage (sizeup/intel/infer.py).
This machine's own addresses (127.0.0.1, ::1, localhost: the local fakes, a local ntfy) are always allowed. The start
logs one line saying so and why; /healthz and Settings > Station > Health show it.

Not armed (tests, tools/*, the supervisor's own process: nothing called arm), everything is allowed as before.
Never anything about the public map feeds (tiles, GIS, trains, weather ...): reads of public data, not ours.
"""

from __future__ import annotations

import os
import threading
import urllib.parse

ENV = "SIZEUP_ALLOW_OUTBOUND"
LOCAL = frozenset({"127.0.0.1", "::1", "localhost"})

_lock = threading.Lock()
_state = {"armed": False, "on": True, "why": ""}


def decide(role: str, env=None) -> tuple[bool, str]:
    """(on, why) for a process of this role in this environment."""
    env = os.environ if env is None else env
    if role == "station":
        return True, "station"
    if str(env.get("SIZEUP_LEASE_FILE", "")).strip():
        return True, "under the hub's supervisor (lease)"
    if str(env.get(ENV, "")).strip() == "1":
        return True, f"{ENV}=1"
    what = "hub" if role == "hub" else "all-in-one"
    return False, f"{what} without the supervisor's lease and without {ENV}=1"


def arm(role: str, env=None) -> str:
    """Decide for this process (sizeup/app.py main). The line to log."""
    on, why = decide(role, env)
    with _lock:
        _state.update(armed=True, on=on, why=why)
    if on:
        return f"outbound: on ({why})"
    return (f"outbound: OFF ({why}): no push, no AWS, no Polly, no Bedrock; this machine's own addresses only. "
            f"Production runs under the supervisor; set {ENV}=1 to allow it here")


def disarm() -> None:
    """Back to not armed (tests)."""
    with _lock:
        _state.update(armed=False, on=True, why="")


def status() -> dict:
    """{armed, on, why}: for /healthz and the Health panel."""
    with _lock:
        return dict(_state)


def local(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in LOCAL


def check(what: str, url: str = "") -> str:
    """"" when `what` may go to url now, else why not (the caller raises its own error with it)."""
    with _lock:
        if not _state["armed"] or _state["on"]:
            return ""
        why = _state["why"]
    if url and local(url):
        return ""
    return f"{what}: outbound off ({why})"
