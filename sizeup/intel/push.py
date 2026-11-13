"""The push for an intel event: composed from an allow-list of fields, then scrubbed, failing closed.

    Title  "PD · Smoke / fire reported · Port Chester"         the pack's label, the category's, the town
    Body   "100 Example Ave · 14:32 · high"                    the gazetteer's address (exact when known: user,
                                                               2026-11-12), the time, the level; "(unconfirmed)" when
                                                               the words don't confirm it; "location not heard"
    Tags   the category's tag, police_car (a police pack)      Priority: the category's
    Click  <public url>/radio?view=police&intel=<id>           behind Cloudflare Access

Never in it: transcript words, a talker, a roster alias, a radio ID, a plate, a date of birth, a person's name. Only
the fields above go in, and scrub() checks the result anyway: any of the event's transmissions' talkers / radio IDs /
units, a 6+ digit number (a radio ID), a plate-like letters-and-digits token, a date: the push is not sent and the
event says suppressed "scrub" (logged without the text).
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b(?:dob|d\.o\.b|born)\b", re.I)
RADIO_ID = re.compile(r"\b\d{6,}\b")
PLATE_LIKE = re.compile(r"\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{5,8}\b")
DOMAIN_TAG = {"police": "police_car", "fire": "fire_engine"}


def _label_body(label: str, town: str) -> str:
    """The address without its town when the title already says it ("100 Example Ave, Port Chester" -> "100 Example
    Ave")."""
    lab = (label or "").strip()
    if town and lab.lower().endswith(", " + town.lower()):
        lab = lab[: -(len(town) + 2)]
    return lab


def compose(ev, pack_label: str, cat: dict, public_url: str = "", tz=None) -> dict:
    """{title, body, tags, priority, click} for the event (never the transcript)."""
    town = ev.town or ""
    title = " · ".join(x for x in (pack_label, cat.get("label") or ev.category, town) if x)
    where = _label_body(ev.label_full, town) if ev.precision != "none" else ""
    if where and not ev.confirmed:
        where += " (unconfirmed)"
    when = datetime.fromtimestamp(ev.first_at, tz).strftime("%H:%M") if ev.first_at else ""
    body = " · ".join(x for x in (where or "location not heard", when, ev.level) if x)
    tags = [t for t in (cat.get("tag") or "", DOMAIN_TAG.get(ev.domain, "")) if t]
    click = ""
    if public_url and ev.id is not None:
        view = "police" if ev.domain == "police" else "all"
        click = f"{public_url.rstrip('/')}/radio?view={view}&intel={quote(str(ev.id))}"
    return {"title": title, "body": body, "tags": tags, "priority": int(ev.priority), "click": click}


def scrub(msg: dict, forbidden=()) -> str:
    """"" when the composed push is clean, else why not (a category only, never the text)."""
    text = f"{msg.get('title', '')}\n{msg.get('body', '')}"
    low = text.lower()
    for f in forbidden:
        f = str(f or "").strip()
        # a number alone is caught by RADIO_ID (6+ digits); a short one may be the house number itself
        if len(f) >= 3 and re.search(r"[A-Za-z]", f) and re.search(rf"(?<![a-z0-9]){re.escape(f.lower())}(?![a-z0-9])", low):
            return "talker"
    if RADIO_ID.search(text):
        return "radio_id"
    if DATE.search(text):
        return "date"
    if PLATE_LIKE.search(text):
        return "plate"
    return ""
