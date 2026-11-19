"""One message -> what a phone gets (#563, docs/mobile-api.md section 4 "Content rules").

NOTHING FREE-TEXT REACHES A PHONE. The caller's body is free text for several kinds (an alarm's body carries what was
heard, a radio ID, the units named and who acknowledged; a health alert's carries serials and details), and no check
can tell a name from a word. So the phone's text is built from an ALLOW-LIST, per kind (device_text):

    alerts            title = a headline rebuilt from fixed words (headline()) plus the structured Who: the units on the
                      CAD's list, the channel, the CAD event number, the address if it has the strict shape
                      (notify.compose); body = that address, else a generic line. Never the caller's body, the
                      talker, the radio ID, the dongle, the detail.
    dispatch          generic title and body until a producer exists (none does): nothing is known to be safe.
    status            the alert's fixed sentence without its configured names (STATUS_TITLES); body always generic.
    intel             title only when every " \u00b7 " part is a loaded rule pack's label (pack or category) or a town
                      (the intel_labels hook, wire.intel_labels: sizeup/intel/push.py compose builds it from exactly
                      those), else generic; body: only the pieces of the engine's composed body (place, time, level;
                      never a transcript) that look like what they should: a time, a level word, "location not
                      heard", a place with a number or a street word. Anything else, and its Listen link, is dropped.

The address (place_ok): by SHAPE only, since a dispatcher types it (sizeup/cad/service.py, up to 200 characters). A house
number (required), an optional direction, up to three capitalised street-name words, a street type, an optional
", Town": at most 50 characters, nothing after the street type but the town (so no digit group, no parentheses, no
trailing words). Anything else is not sent: the body is generic and the title has no address. A unit's display name
(unit_name_ok): at most 30 characters of letters, digits and " .'-", no run of four digits, no "@"; else the code alone.

Where each title comes from (read in the code; the device title is rebuilt from FIXED words, never copied):
    alarms     sizeup/cad/alarms.py `_push`: `what(a).upper()` = "EMERGENCY BUTTON (<via>)" and the matched KEYWORDS
               (config, never the transcript), or "AIRCRAFT EMERGENCY: SQUAWK ..." (adsb), with DRILL / Ended /
               Acknowledged. headline() keeps only the segments it knows; the rest is dropped.
    airport    sizeup/cad/airport_alerts.py `_push` sends `r["title"]`: built by sizeup/atc.py `_title`,
               airport_watch.py `level_title` and `_alert`, airport_alerts.py `describe` from ATC speech (callsigns, a
               place), the rule packs and ADS-B; `update()` takes `title[:200]` from those same sources (no dispatcher
               edits it). Only the fixed lead is kept ("HPN ATC pre-alert", "HPN Alert N", "AIRCRAFT EMERGENCY"):
               never the callsign, type or place.
    status     sizeup/cloud/health.py `_push`: `a["title"]` is a fixed sentence in code plus configured names (channel
               labels, dongle names, jobs, groups, databases); STATUS_TITLES keeps the sentence and drops the name, and
               the Who (station name, detail) is not sent at all.
    intel      sizeup/intel/push.py `compose`: the pack's label, the category's label (or key), the town.
    dispatch   no producer yet.

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
INTEL_FIXED = re.compile(r"^(\d{1,2}:\d{2}|low|medium|high|location not heard|#\d{2}-\d{6}"
                         r"|FD (?:on scene|dispatched|there|here(?: \d{1,2}:\d{2})?))$", re.I)
PHONE = re.compile(r"\b\d{3}[-. ]?\d{3}[-. ]\d{4}\b|\b\d{3}[-.]\d{4}\b")
ID_OK = re.compile(r"[\w.:-]{1,64}")                      # a reference the app may be handed (an event number)
INTEL_ID = re.compile(r"[?&]intel=([\w.-]{1,40})")
_OPEN = "Open sizeup for details"
GENERIC = {"alerts": ("sizeup alert", _OPEN), "intel": ("Radio pre-alert", _OPEN), "status": ("System alert", _OPEN),
           "dispatch": ("Dispatch", _OPEN)}


def _priority(p) -> int:
    try:
        return notify._priority(p)
    except Exception:  # noqa: BLE001
        return 3


PLAIN = re.compile(r"[A-Za-z0-9 .,:;'\u2019()/#+&%\u00b7\u00d7\u2013\u2014\u2026-]{1,100}")
# alarms.KEYWORDS, upper-cased, "..." as the ellipsis
FIXED_KEYWORDS = ("MAYDAY", "MAY DAY MAY DAY", "EMERGENCY TRAFFIC", "FIREFIGHTER DOWN", "FIRE FIGHTER DOWN",
                  "MEMBER DOWN", "LOST FIREFIGHTER", "LOST FIRE FIGHTER",
                  "PAR \u2026 MISSING", "PAR \u2026 UNACCOUNTED")
VIAS = ("MDC", "P25", "FLEETSYNC")
FLAGS = re.compile(r"^(DRILL: )?(Ended: |Acknowledged: )?")
# sizeup/cloud/health.py: each title it builds, with the configured name in it (a channel label, a dongle name, a job, a
# group, a database) left out. (regex on the title, text sent). Anything not here is the generic status title.
STATUS_TITLES = (
    (r"Station silent", "Station silent"), (r"Station update failed( and its rollback too)?", "Station update failed"),
    (r"Station behind the hub", "Station behind the hub"), (r"Takeover checks skipped", "Takeover checks skipped"),
    (r"Litestream too old", "Litestream too old"), (r"Speech-to-text (down|behind)", "Speech-to-text problem"),
    (r"ADS-B receiver down", "ADS-B receiver down"), (r"trunk-recorder down", "trunk-recorder down"),
    (r"Station disk full", "Station disk full"), (r"Station's events backing up", "Station's events backing up"),
    (r"P25 trunk silent", "P25 trunk silent"), (r"Amazon Transcribe failing", "Amazon Transcribe failing"),
    (r"Amazon Transcribe at \d{1,3}% of monthly limit", "Amazon Transcribe near its monthly limit"),
    (r"Amazon Transcribe monthly limit reached", "Amazon Transcribe monthly limit reached"),
    (r"Reception test: job not restored", "Reception test: job not restored"),
    (r"Antenna groups dropped", "Antenna groups dropped"),
    (r"Antenna group .+ (short|check)", "Antenna group needs a check"),
    (r"Duplicate dongle serial .+", "Duplicate dongle serial"), (r"SDR .+ gone", "An SDR is gone"),
    (r".+ not running", "A radio job is not running"), (r".+ stuck open", "A radio is stuck open"),
    (r"Replica (corrupt|follower failing|not verifiable): .+", "Replica problem"),
    (r"Replica not verifiable: .+", "Replica problem"),
)


def title_ok(title: str) -> bool:
    """A title written by code (a health alert, an intel label): plain characters, a sane length, no link."""
    return bool(PLAIN.fullmatch(title or "")) and not URL.search(title)


def headline(kind: str, title: str, intel_labels=None) -> str | None:
    """The fixed headline for a title, or None (then the generic one). alerts: rebuilt from known words only (module
    doc); status: STATUS_TITLES; intel: only a title whose every " \u00b7 " part is a label of the loaded rule packs or
    a town (intel_labels), as sizeup/intel/push.py compose builds it; dispatch: always None (no producer yet, so
    nothing is known to be safe: its device title stays generic until one exists)."""
    title = str(title or "").strip()
    if kind == "dispatch":
        return None
    if kind == "intel":
        if re.fullmatch(r"\d{1,4} more pre-alerts \(over the hourly limit\)", title):     # the engine's digest
            return title
        model = title.endswith(" \u00b7m")              # the local model raised it (sizeup/intel/push.py compose)
        core = title[:-3] if model else title
        fyi = core.startswith("FYI \u00b7 ")              # a fire event with the FD already there
        parts = (core[6:] if fyi else core).split(" \u00b7 ")
        ok = intel_labels is not None and 2 <= len(parts) <= 3 and all(p in intel_labels for p in parts)
        return title if ok else None
    if kind == "status":
        m = re.match(r"^(Still: |Recovered: )?(.*?)( \d+ s| \d+ min| \d+ h \d+ min)?$", title)
        for pat, out in STATUS_TITLES:
            if m and re.fullmatch(pat, m.group(2)):
                return f"{m.group(1) or ''}{out}{m.group(3) or ''}"
        return None
    if kind != "alerts":
        return None
    m = FLAGS.match(title)
    flags, rest = m.group(0), title[m.end():]
    if rest.upper().startswith("AIRCRAFT EMERGENCY"):
        squawk = re.search(r"SQUAWK (7500|7600|7700)\b", rest.upper())
        return flags + "AIRCRAFT EMERGENCY" + (f": SQUAWK {squawk.group(1)}" if squawk else "")
    if rest.startswith("HPN ATC"):
        return flags + "HPN ATC pre-alert"
    lv = re.match(r"HPN Alert ([123])\b", rest)
    if lv:
        return flags + f"HPN Alert {lv.group(1)}"
    known = []
    for seg in rest.split(" + "):
        seg = seg.strip().upper()
        via = re.fullmatch(r"EMERGENCY BUTTON(?: \(([A-Z0-9]+)\))?", seg)
        if via and (via.group(1) in (None, *VIAS)):
            known.append(seg)
        elif seg in FIXED_KEYWORDS:
            known.append(seg)
    return (flags + " + ".join(known)) if known else None


STREET_TYPES = ("St", "Street", "Ave", "Avenue", "Rd", "Road", "Blvd", "Boulevard", "Dr", "Drive", "Ln", "Lane",
                "Ct", "Court", "Pl", "Place", "Pkwy", "Parkway", "Hwy", "Highway", "Way", "Ter", "Terrace", "Cir",
                "Circle", "Sq", "Square", "Trl", "Trail", "Tpke", "Turnpike", "Expy", "Expressway", "Pike", "Row", "Path",
                "Walk")
# A house number (required), an optional direction, at most 3 capitalised street-name words, a street type as the last
# word, an optional ", Town". Every quantifier is bounded and the length is capped first: linear on any input.
ADDRESS = re.compile(r"\d{1,5}[A-Za-z]?\s+(?:[NSEW]\.?\s+)?(?:[A-Z][A-Za-z'-]{0,24}\s+){0,3}(?:%s)\.?"
                     r"(?:,\s*[A-Z][A-Za-z .'-]{1,24})?" % "|".join(STREET_TYPES))
ADDRESS_MAX = 50
UNIT_NAME = re.compile(r"[A-Za-z0-9 .'-]{1,30}")


def place_ok(address: str) -> bool:
    """An address a phone may be told, by SHAPE only: a house number, an optional direction, up to three capitalised
    street-name words, a street type, an optional ", Town"; at most 50 characters; nothing after the street type but the
    town, so no digits (no radio ID or split phone number), no parentheses, no trailing words. An address of any other
    shape ("Example St Jane Doe", "John Smith St" with no house number, an intersection) is the generic body: the app
    fetches the place after unlock. The ", Town" tail must be a town of the county's list (data/westchester_towns.txt, read
    once by sizeup/cad/sources.py county_towns; the lookup is a dict of about 50 names). The street name is checked by
    shape only: a name-shaped street WITH a house number ("123 Casey Rivera Ave") can pass (a stated residual)."""
    a = (address or "").strip()
    if not 0 < len(a) <= ADDRESS_MAX or ADDRESS.fullmatch(a) is None:
        return False
    if ", " in a:
        from sizeup.intel.rules import town_names
        return a.rsplit(", ", 1)[1].strip().lower() in town_names()
    return True


def unit_name_ok(name: str) -> bool:
    """The CAD list's display name for a unit: at most 30 characters of letters, digits, space . ' -, no run of four digits
    and no "@" (a name typed into the list can be anything: otherwise the code is sent alone)."""
    return UNIT_NAME.fullmatch(name or "") is not None and not re.search(r"\d{4}", name) and "@" not in name


def _who(kind: str, who, unit_of=None):
    """The Who a phone may be told about: the units that are really on the CAD's list, the channel, the event number and
    the address that passes place_ok. Never the talker, the dongle, the station, the detail, a radio ID or an alias.
    unit_of(code) -> the list's own name, or None for a code that is not a unit (None: no unit passes)."""
    w = notify.Who.of(who)
    if w is None:
        return None, ""
    units = []
    for u in w.units:
        token = (u[0] if u else "").split()[:1]
        code = token[0] if token else ""
        name = unit_of(code) if unit_of is not None and code and not code.isdigit() else None
        if name is not None and (code, name if unit_name_ok(name) else "") not in units:
            units.append((code, name if unit_name_ok(name) else ""))
    addr = w.address if place_ok(w.address) else ""
    return (dataclasses.replace(w, talker="", dongle="", detail="", station="", units=tuple(units), address=addr),
            w.talker.strip())


STREET_ONLY = re.compile(r"(?:[NSEW]\.?\s+)?(?:[A-Z][A-Za-z'-]{0,24}\s+){1,3}(?:%s)\.?" % "|".join(STREET_TYPES))


def intel_place_ok(seg: str) -> bool:
    """A place as sizeup/intel/push.py where_text says it: an exact address (place_ok), or the street alone or with its
    block ("Example Ave, 100 blk"), each optionally "(unconfirmed)": the same shape rules, at most 60 characters."""
    s = re.sub(r" \(unconfirmed\)$", "", seg.strip())
    if not 0 < len(seg) <= 60:
        return False
    s = re.sub(r", \d{2,4} blk$", "", s)
    return place_ok(s) or STREET_ONLY.fullmatch(s) is not None


def _intel_body(body: str) -> str:
    """The pieces of an intel body a phone may get (module doc), in their order, joined as the engine joins them: a time,
    a level, "location not heard", the FD line and event number of an FYI, or a place that passes intel_place_ok (the
    same shape rule as an alert's address: a name next to a street word is not enough)."""
    segs = [x.strip() for x in LISTEN_LINE.sub("", str(body or "")).replace("\n", " \u00b7 ").split(" \u00b7 ")]
    return " \u00b7 ".join(x for x in segs if x and not URL.search(x) and (INTEL_FIXED.match(x) or intel_place_ok(x)))


def device_text(kind: str, title: str, body: str, who=None, unit_of=None, intel_labels=None) -> tuple[str, str, str]:
    """(title, body, why): the text a phone may get, from the allow-list in the module doc. why is "" when it passed
    the scrub, else a category (never the text) and the text is the generic one."""
    w, talker = _who(kind, who, unit_of)
    head = headline(kind, title, intel_labels)
    if head is None:
        return GENERIC[kind][0], GENERIC[kind][1], "title"
    t = URL.sub("", notify.compose(head, w)).strip()[:TITLE_MAX]
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
    if ID_OK.fullmatch(str(ref or "")):
        return str(ref)
    w = notify.Who.of(who)
    if w is not None and ID_OK.fullmatch(w.incident.strip()):
        return w.incident.strip()
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
