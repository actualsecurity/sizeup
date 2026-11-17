"""PUT and DELETE /api/v1/devices/{id}/push (#563, docs/mobile-api.md section 4): a device registers (or replaces) its
own push endpoint, and removes it. The app sends its platform token and, for iOS, which Apple environment the build is for
(`sandbox` for debug builds, `production` for TestFlight and the store): the hub keeps the SNS endpoint made for it, per
kind preferences and quiet hours (sizeup/push/). The token and the endpoint are never answered back.

Needs a signed-in device (need "device", flag "push", role "read"), and a device registers only ITSELF: the {id} must be
the principal's own device id (another's: 403). Push not set up on the hub (no SNS platform application for that
platform): 503 push_unavailable, nothing stored. The service is installed by the hub's start (sizeup/push/wire.py); none
installed answers the same 503. A body over MAX_BODY (this route's own cap, below the server's) is a 413. This module
imports no database: the store is sizeup/push/devices.py."""
from __future__ import annotations

import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sizeup.push import service as svc
from sizeup.push.service import HM

MAX_BODY = 4096
KEYS = {"platform", "token", "environment", "prefs", "quiet"}


def _err(status: int, code: str, message: str) -> tuple[int, dict]:
    """A handler answers (status, object): the envelope as a dict (errors.body makes the bytes the server sends)."""
    return status, {"error": {"code": code, "message": message}}


def _me(ctx):
    """None when this device may act on {id}, else the 403."""
    if ctx.params.get("id") != getattr(ctx.principal, "device", None):
        return _err(403, "forbidden", "a device registers only itself")
    return None


def _unavailable():
    return _err(503, "push_unavailable", "push is not set up on this hub")


def _quiet(q):
    """{"from": "22:00", "to": "06:00", "tz": "America/New_York"} or None."""
    if q is None:
        return None
    if not isinstance(q, dict) or set(q) != {"from", "to", "tz"} or not all(isinstance(v, str) for v in q.values()):
        raise ValueError("quiet: {from, to, tz} or null")
    if not (HM.fullmatch(q["from"]) and HM.fullmatch(q["to"])):
        raise ValueError("quiet: from and to are HH:MM")
    try:
        ZoneInfo(q["tz"])
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError("quiet: tz is an IANA zone name") from None
    return {"from": q["from"], "to": q["to"], "tz": q["tz"]}


def parse(raw: bytes) -> dict:
    """The registration body, checked: {platform, environment, token, prefs, quiet}. ValueError(message)."""
    try:
        d = json.loads(raw or b"")
    except ValueError:
        raise ValueError("body: JSON") from None
    if not isinstance(d, dict) or set(d) - KEYS:
        raise ValueError("body: an object with platform, token, environment, prefs, quiet")
    platform, env, token = d.get("platform"), d.get("environment"), d.get("token")
    if platform not in svc.PLATFORMS:
        raise ValueError("platform: ios or android")
    if not isinstance(token, str) or not svc.TOKEN.fullmatch(token):
        raise ValueError("token: the platform's push token")
    if platform == "ios" and env not in svc.ENVIRONMENTS:
        raise ValueError("environment: sandbox or production (required for ios)")
    if platform == "android" and env is not None:
        raise ValueError("environment: only for ios")
    prefs = d.get("prefs")
    if prefs is not None and (not isinstance(prefs, dict) or set(prefs) - set(svc.DEFAULT_PREFS)
                              or not all(isinstance(v, bool) for v in prefs.values())):
        raise ValueError("prefs: true or false for alerts, intel, status, dispatch")
    return {"platform": platform, "environment": env or "", "token": token, "prefs": prefs,
            "quiet": _quiet(d.get("quiet"))}


def _shown(row: dict) -> dict:
    return {"device": row["device_id"], "platform": row["platform"], "environment": row["env"] or None,
            "prefs": {k: row["prefs"].get(k, v) for k, v in svc.DEFAULT_PREFS.items()}, "quiet": row["quiet"],
            "updated": row["updated"]}


def put_push(ctx) -> tuple[int, dict]:
    if (no := _me(ctx)) is not None:
        return no
    service = svc.installed()
    if service is None:
        return _unavailable()
    if len(ctx.body) > MAX_BODY:
        return _err(413, "body_too_large", f"the body is over {MAX_BODY} bytes")
    try:
        d = parse(ctx.body)
    except ValueError as e:
        return _err(400, "bad_request", str(e))
    try:
        row = service.register(ctx.principal.device, ctx.principal.email, d["platform"], d["environment"], d["token"],
                               d["prefs"], d["quiet"])
    except svc.PushError as e:
        return _err(e.status, e.code, e.message)
    return 200, _shown(row)


def delete_push(ctx) -> tuple[int, dict]:
    if (no := _me(ctx)) is not None:
        return no
    service = svc.installed()
    if service is None:
        return _unavailable()
    device = ctx.principal.device
    return 200, {"device": device, "registered": False, "removed": service.unregister(device)}
