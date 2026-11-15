"""Outbound off unless this is production (the guard against a hub on a restored copy reaching the real world).

A hub started by hand on a restored production copy (docs/development.md "Maintainers only") holds the real push topics
in its Settings and may have the real AWS credentials and bucket names in its environment. One forgotten step and it
pushes to the crew's phones, uploads into the backup bucket or pays for Transcribe. So the app decides once, at its
start (sizeup/app.py main: arm), whether it may reach the project's outside services at all:

    on    the hub under its supervisor (sizeup/handoff.py gives the app SIZEUP_LEASE_FILE: the fenced production hub on
          ECS), a station (role "station", or any config forwarding to a hub through Settings > Station > Hub link:
          forwarding()), or SIZEUP_ALLOW_OUTBOUND=1 (a person's explicit choice, e.g. an all-in-one with its own ntfy
          topic)
    off   anything else started with `python -m sizeup`: all-in-one, or a hub without a lease

Off, every outbound path refuses before a byte leaves (check(): its own error, the reason in it): ntfy publishes
(sizeup/notify.py publish: alerts, status, intel, the replica watch's and the error watch's pushes), every AWS request
(sizeup/aws.py sign / presign / json_call: S3 audio, attachments, documents, map caches, CloudWatch Logs, SSM,
Transcribe and its streaming, the Listen links), Polly (sizeup/polly.py), the Bedrock stage (sizeup/intel/infer.py), and
the phone pre-alert's read-only subscription (sizeup/prealert.py: the department's real ring topic). A refused push is
NotifyOff: dropped at once, never retried.
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
_basis: dict = {"role": "", "env": {}}          # what arm() decided on, for forwarding()


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


def _line(on: bool, why: str) -> str:
    if on:
        return f"outbound: on ({why})"
    return (f"outbound: OFF ({why}): no push, no AWS, no Polly, no Bedrock; this machine's own addresses only. "
            f"Production runs under the supervisor; set {ENV}=1 to allow it here")


def arm(role: str, env=None) -> str:
    """Decide for this process (sizeup/app.py main). The line to log."""
    env = dict(os.environ if env is None else env)
    on, why = decide(role, env)
    with _lock:
        _state.update(armed=True, on=on, why=why)
        _basis.update(role=role, env=env)
    return _line(on, why)


def forwarding(on: bool) -> str:
    """A config without role = "station" forwarding to a hub anyway (Settings > Station > Hub link, state/cloud.json:
    sizeup/cloud/stationmode.py, at its start and at every switch) is a station here, and back to its own role when
    switched off. The line to log when that changes the decision, else "" (not armed: always "")."""
    with _lock:
        if not _state["armed"]:
            return ""
        was = _state["on"]
        now, why = decide("station" if on else _basis["role"], _basis["env"])
        if on and now and _basis["role"] != "station":
            why = "forwarding to a hub (Settings > Station > Hub link)"
        _state.update(on=now, why=why)
    return _line(now, why) if now != was else ""


def disarm() -> None:
    """Back to not armed (tests)."""
    with _lock:
        _state.update(armed=False, on=True, why="")
        _basis.update(role="", env={})


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
