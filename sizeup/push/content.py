"""One message -> what a phone gets (#563, docs/mobile-api.md section 4 "Content rules").

The title and body are the ones ntfy gets, composed by the same `notify.compose` and checked by the same `scrub()` as the
intel pushes (sizeup/intel/push.py): no names, talkers, radio IDs, plates or dates. Exact addresses stay. Before that
check the pieces that never go to a phone are removed: the Who's talker and dongle serial, every link (a presigned
Listen URL is the ntfy path's own) and the "Listen:" line. A message that still fails the check is not dropped (a
mayday must page) but replaced by a generic one that says only the kind: the app fetches the details after unlock.

The payload carries `{kind, id, title, body}` and nothing more (it crosses Apple's and Google's infrastructure):
    APNs   {"aps": {"alert": {"title", "body"}, "sound", "interruption-level", "thread-id"}, "kind", "id"}
    FCM    a high-priority data message {"kind", "id", "title", "body"}

Interruption level (iOS): time-sensitive by default (no entitlement; breaks through Focus, not the mute switch);
critical, with the `sound` dictionary, only for dispatch and for an emergency alert (priority 5) AND only when the
entitlement is configured (SIZEUP_PUSH_CRITICAL=1, Apple's approval); intel and status are active (passive for a
priority 2 status). SNS must pass `interruption-level`, the sound dictionary and the `apns-*` attributes through
unchanged: unproven until a real sandbox build receives one (docs/mobile-api.md "Could not verify")."""
from __future__ import annotations

import dataclasses
import json
import re
import secrets

from sizeup import notify
from sizeup.intel.push import scrub

KINDS = ("alerts", "intel", "status", "dispatch")
CRITICAL_KINDS = frozenset({"alerts", "dispatch"})     # quiet hours never hold these
TITLE_MAX, BODY_MAX = 120, 240
URL = re.compile(r"https?://\S+", re.I)
LISTEN_LINE = re.compile(r"^\s*Listen:.*$", re.I | re.M)
INCIDENT_NO = re.compile(r"\b\d{2}-\d{6}\b")            # the CAD's event number: a reference, not a radio ID
INTEL_ID = re.compile(r"[?&]intel=([\w.-]{1,40})")
GENERIC = {"alerts": ("sizeup alert", "Open sizeup for details"), "intel": ("Radio pre-alert", "Open sizeup for details"),
           "status": ("System alert", "Open sizeup for details"), "dispatch": ("Dispatch", "Open sizeup for details")}


def _priority(p) -> int:
    try:
        return notify._priority(p)
    except Exception:  # noqa: BLE001
        return 3


def clean(title: str, body: str, who=None) -> tuple[str, str, str]:
    """(title, body, why): the text a phone may get, or the generic one and why ("" when it passed unchanged)."""
    w = notify.Who.of(who)
    talker = ""
    if w is not None:
        talker = w.talker.strip()
        w = dataclasses.replace(w, talker="", dongle="")
    t = notify.compose(str(title), w)
    b = URL.sub("", LISTEN_LINE.sub("", str(body or ""))).strip()
    t, b = URL.sub("", t).strip()[:TITLE_MAX], b[:BODY_MAX]
    masked = {"title": INCIDENT_NO.sub("#", t), "body": INCIDENT_NO.sub("#", b)}
    return t, b, scrub(masked, [talker] if talker else ())


def message_id(kind: str, click: str = "", who=None, ref: str = "") -> str:
    """The reference the app fetches the details with: the caller's, else the CAD event number, else the intel event in
    the click link, else a random one (a status alert has none)."""
    if ref:
        return str(ref)[:64]
    w = notify.Who.of(who)
    if w is not None and w.incident.strip():
        return w.incident.strip()[:64]
    m = INTEL_ID.search(click or "")
    if m:
        return f"intel:{m.group(1)}"
    return f"{kind}:{secrets.token_hex(6)}"


def level(kind: str, priority, critical_ok: bool) -> str:
    p = _priority(priority)
    if kind == "dispatch" or (kind == "alerts" and p >= 5):
        return "critical" if critical_ok else "time-sensitive"
    if kind == "alerts":
        return "time-sensitive"
    if kind == "intel":
        return "time-sensitive" if p >= 4 else "active"
    return "passive" if p <= 2 else "active"


def apns(kind: str, mid: str, title: str, body: str, lvl: str) -> dict:
    aps: dict = {"alert": {"title": title, "body": body}, "interruption-level": lvl, "thread-id": kind}
    if lvl == "critical":
        aps["sound"] = {"critical": 1, "name": "default", "volume": 1.0}
    elif lvl != "passive":
        aps["sound"] = "default"
    return {"aps": aps, "kind": kind, "id": mid}


def apns_attributes(lvl: str) -> dict[str, str]:
    """The SNS message attributes for APNs (apns-push-type, apns-priority, apns-expiration): 10 now, 5 for passive."""
    return {"AWS.SNS.MOBILE.APNS.PUSH_TYPE": "alert", "AWS.SNS.MOBILE.APNS.PRIORITY": "5" if lvl == "passive" else "10",
            "AWS.SNS.MOBILE.APNS.TTL": "900"}


def fcm(kind: str, mid: str, title: str, body: str, lvl: str) -> dict:
    """FCM HTTP v1 through an SNS platform application: a data-only message, high priority unless passive."""
    return {"fcmV1Message": {"message": {"data": {"kind": kind, "id": mid, "title": title, "body": body},
                                         "android": {"priority": "normal" if lvl == "passive" else "high",
                                                     "ttl": "900s"}}}}


def message(platform: str, env: str, payload: dict) -> dict[str, str]:
    """The SNS Message (MessageStructure=json): the platform's key, and the default every such message needs."""
    key = "GCM" if platform == "android" else ("APNS_SANDBOX" if env == "sandbox" else "APNS")
    default = (payload["fcmV1Message"]["message"]["data"]["title"] if key == "GCM" else payload["aps"]["alert"]["title"])
    return {"default": str(default or ""), key: json.dumps(payload, separators=(",", ":"))}
