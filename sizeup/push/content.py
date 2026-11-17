"""One message -> what a phone gets (#563, docs/mobile-api.md section 4 "Content rules").

NOTHING FREE-TEXT REACHES A PHONE. The caller's body is free text for several kinds (an alarm's body carries what was
heard, a radio ID, the units named and who acknowledged; a health alert's carries serials and details), and no check
can tell a name from a word. So the phone's text is built from an ALLOW-LIST, per kind (device_text):

    alerts, dispatch  title = the headline the code chose ("MAYDAY", "Acknowledged: MAYDAY", a call type) plus the
                      structured Who: units, channel, the CAD event number, the address (notify.compose);
                      body = the address, else a generic line. Never the caller's body, the talker, the radio ID,
                      the dongle, the detail.
    status            title as above (a system sentence the code wrote); body is always the generic line.
    intel             title (the rule pack's and category's labels, the town); body: only the pieces of the engine's
                      composed body (sizeup/intel/push.py: place, time, level; never a transcript) that look like what
                      they should: a time, a level word, "location not heard", a place with a number or a street word.
                      Anything else, and its Listen link, is dropped. (A place is the gazetteer's label: a street named
                      after a person passes, which ntfy says too.)

Then the intel `scrub()` (radio IDs of 6+ digits, plate-like tokens, dates) runs over what is left, as a last net
under the allow-list: a text that still fails is replaced by the generic one (a mayday must still page), never sent.
Links never go: a presigned Listen URL is the ntfy path's own. Exact addresses stay, as in ntfy.

The payload carries `{kind, id, title, body}` and nothing more (it crosses Apple's and Google's infrastructure):
    APNs   {"aps": {"alert": {"title", "body"}, "sound", "interruption-level", "thread-id"}, "kind", "id"}
    FCM    a high-priority data message {"kind", "id", "title", "body"}

Interruption level (iOS): time-sensitive by default (no entitlement; breaks through Focus, not the mute switch);
critical, with the `sound` dictionary, only for dispatch and for an emergency alert (priority 5) AND only when the
entitlement is configured (SIZEUP_PUSH_CRITICAL=1, Apple's approval); intel and status are active (passive for a
priority 2 status).

UNPROVEN until tried on a real sandbox iOS build and a real Android device: that SNS passes `interruption-level`, the
critical `sound` dictionary and the `AWS.SNS.MOBILE.APNS.*` message attributes through to APNs unchanged, and the FCM v1
wrapper's shape in fcm() (`fcmV1Message` > `message` > `data` / `android`, written from memory: a wrong key means
Android silently receives nothing)."""
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
TTL_S = 900                                            # a page older than this is not delivered late (APNs, FCM)
URL = re.compile(r"https?://\S+", re.I)
LISTEN_LINE = re.compile(r"^\s*Listen:.*$", re.I | re.M)
INCIDENT_NO = re.compile(r"\b\d{2}-\d{6}\b")            # the CAD's event number: a reference, not a radio ID
INTEL_FIXED = re.compile(r"^(\d{1,2}:\d{2}|low|medium|high|location not heard)$", re.I)
PHONE = re.compile(r"\b\d{3}[-. ]?\d{3}[-. ]\d{4}\b|\b\d{3}[-.]\d{4}\b")
PLACE = re.compile(r"^\d+[A-Za-z]?\s+[A-Za-z]|\b(?:st|street|ave|avenue|rd|road|blvd|dr|drive|ln|lane|ct|court|pl|"
                   r"place|pkwy|parkway|hwy|way|ter|terrace|cir|circle|exit|route|rt|bridge|tpke|turnpike|expy|sq|"
                   r"square|blk)\b", re.I)
INTEL_ID = re.compile(r"[?&]intel=([\w.-]{1,40})")
_OPEN = "Open sizeup for details"
GENERIC = {"alerts": ("sizeup alert", _OPEN), "intel": ("Radio pre-alert", _OPEN), "status": ("System alert", _OPEN),
           "dispatch": ("Dispatch", _OPEN)}


def _priority(p) -> int:
    try:
        return notify._priority(p)
    except Exception:  # noqa: BLE001
        return 3


def _who(kind: str, who):
    """The Who a phone may be told about: the units, channel, event number and address (and, for status only, the
    station). Never the talker, the dongle, the detail."""
    w = notify.Who.of(who)
    if w is None:
        return None, ""
    keep = w.station if kind == "status" else ""
    return dataclasses.replace(w, talker="", dongle="", detail="", station=keep), w.talker.strip()


def _intel_body(body: str) -> str:
    """The pieces of an intel body a phone may get (module doc), in their order, joined as the engine joins them."""
    segs = [x.strip() for x in LISTEN_LINE.sub("", str(body or "")).replace("\n", " \u00b7 ").split(" \u00b7 ")]
    return " \u00b7 ".join(x for x in segs if x and not URL.search(x) and (INTEL_FIXED.match(x) or PLACE.search(x)))


def device_text(kind: str, title: str, body: str, who=None) -> tuple[str, str, str]:
    """(title, body, why): the text a phone may get, from the allow-list in the module doc. why is "" when it passed
    the scrub, else a category (never the text) and the text is the generic one."""
    w, talker = _who(kind, who)
    t = URL.sub("", notify.compose(str(title), w)).strip()[:TITLE_MAX]
    if kind == "intel":
        b = _intel_body(body)[:BODY_MAX]
    elif kind in ("alerts", "dispatch") and w is not None and w.address.strip():
        b = w.address.strip()[:BODY_MAX]
    else:
        b = GENERIC[kind][1]
    masked = {"title": INCIDENT_NO.sub("#", t), "body": INCIDENT_NO.sub("#", b)}
    why = scrub(masked, [talker] if talker else ()) or ("phone" if PHONE.search(f"{t}\n{b}") else "")
    if why or not t:
        return GENERIC[kind][0], GENERIC[kind][1], why or "empty"
    return t, b or GENERIC[kind][1], ""


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


def platform_key(platform: str, env: str) -> str:
    """The SNS platform a device is on: GCM (android), APNS_SANDBOX or APNS (ios, by its environment)."""
    return "GCM" if platform == "android" else ("APNS_SANDBOX" if env == "sandbox" else "APNS")


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
            "AWS.SNS.MOBILE.APNS.TTL": str(TTL_S)}


def fcm(kind: str, mid: str, title: str, body: str, lvl: str) -> dict:
    """FCM HTTP v1 through an SNS platform application: a data-only message, high priority unless passive. UNPROVEN
    (module doc): the wrapper keys are from memory."""
    return {"fcmV1Message": {"message": {"data": {"kind": kind, "id": mid, "title": title, "body": body},
                                         "android": {"priority": "normal" if lvl == "passive" else "high",
                                                     "ttl": f"{TTL_S}s"}}}}


def message(platform: str, env: str, payload: dict) -> dict[str, str]:
    """The SNS Message (MessageStructure=json): the platform's key, and the default every such message needs."""
    key = platform_key(platform, env)
    if key == "GCM":
        default = payload["fcmV1Message"]["message"]["data"]["title"]
    else:
        default = payload["aps"]["alert"]["title"]
    return {"default": str(default or ""), key: json.dumps(payload, separators=(",", ":"))}
