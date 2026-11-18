"""The push for an intel event: composed from an allow-list of fields, then (push_pii off) scrubbed, failing closed.

    Title  "PD · Smoke / fire reported · Port Chester"         the pack's label, the category's, the town
    Body   "100 Example Ave · 14:32 · high"                    the gazetteer's address when the words confirm it
                                                               (user, 2026-11-12), the time, the level; unconfirmed: only
                                                               the street and block ("Example Ave, 100 blk") and
                                                               "(unconfirmed)"; a number the transcript says after a
                                                               plate / ID / unit / DOB word: the street alone (fail
                                                               closed); "location not heard"
    Tags   the category's tag, police_car (a police pack)      Priority: the category's
    Click  <public url>/radio?view=police&intel=<id>           behind Cloudflare Access
    FYI    a fire-related event with the FD already there (engine.py _fire): the title starts "FYI · ", the body
           ends "FD on scene · #26-000123" (fd_text), its priority the pack's fyi / safety priority. The FD
           cleared (a rekindle): a pre-alert as usual, its body ending "FD here 14:05 · #26-000123"

Not among its fields: transcript words, a talker, a roster alias, a radio ID, a plate, a date of birth, a person's
name. Only the fields above go in, and (push_pii off) scrub() checks the result anyway: any of the event's
transmissions' talkers / radio IDs / units, a 6+ digit number (a radio ID), a plate-like letters-and-digits token, a
date: the push is not sent and the event says suppressed "scrub" (logged without the text).

push_pii (Settings > System > Intel, on by default; admin decision, 2026-11-17): intel output is not scrubbed, access
to it is limited instead, so with it on the engine skips scrub() and a name, a plate or another PII token in these
fields passes through. Off: scrub() as above, unchanged. The fields are the same allow-list either way (no transcript
words are added), and the locator's own checks stay (a place label shaped like a plate or an ID is no place:
scrub_label(); a house number the words put after a plate / ID word: the street alone), as they are about where, not
who. ntfy topics are readable by anyone who knows the name (sizeup/notify.py).
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

from sizeup.intel.place import SPACED_PLATE  # noqa: E402  (route labels excepted: "NY 120", "US 1")
HOUSE = re.compile(r"^\s*(\d+)[A-Za-z]?\s+(.+)$")
DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b(?:dob|d\.o\.b|born)\b", re.I)
RADIO_ID = re.compile(r"\b\d{6,}\b")
PLATE_LIKE = re.compile(r"\b(?=[A-Za-z]*\d)(?=\d*[A-Za-z])[A-Za-z0-9]{5,8}\b")
OUR_NUMBER = re.compile(r"#\d{2}-\d{6}\b")
DOMAIN_TAG = {"police": "police_car", "fire": "fire_engine"}


def _label_body(label: str, town: str) -> str:
    """The address without its town when the title already says it ("100 Example Ave, Port Chester" -> "100 Example
    Ave")."""
    lab = (label or "").strip()
    if town and lab.lower().endswith(", " + town.lower()):
        lab = lab[: -(len(town) + 2)]
    return lab


def where_text(ev, doubtful: bool = False) -> str:
    """The place as a push says it: the exact address only when confirmed and not doubtful; else street and block, or
    the street alone."""
    if ev.precision == "none":
        return ""
    where = _label_body(ev.label_full, ev.town or "")
    m = HOUSE.match(where) if ev.precision in ("address", "block") else None
    if m:
        n, street = int(m.group(1)), m.group(2).replace("block of ", "").strip()
        if doubtful:
            where = street
        elif not ev.confirmed or ev.precision == "block":
            where = f"{street}, {n // 100 * 100} blk"
    if not ev.confirmed:
        where += " (unconfirmed)"
    return where


def doubtful_number(label: str, texts) -> bool:
    """The address's house number is one the transcript says right after a plate / ID / unit / DOB word."""
    from sizeup.intel.place import cued_numbers
    m = HOUSE.match(label or "")
    if not m:
        return False
    n = m.group(1)
    return any(re.sub(r"\D", "", tok) == n for t in texts or () for tok in cued_numbers(t))


def category_label(cat: dict, pack_label: str, category: str) -> str:
    """A category's label as shown beside its pack's: without the pack's own label in front ("Area PD: Weapon / shots"
    is unique in Settings; beside "Area PD" it is "Weapon / shots", rules.rules_from)."""
    label = str(cat.get("label") or category)
    pre = f"{pack_label}: " if pack_label else ""
    return label[len(pre):] if pre and label.startswith(pre) and len(label) > len(pre) else label


FD_SHOWN = {"on_scene": "FD on scene", "dispatched": "FD dispatched", "there": "FD there"}
FYI = frozenset(FD_SHOWN)


def fd_text(fd: str, number: str = "", at=None, tz=None) -> str:
    """Event.fd as shown, terse: "FD on scene · #26-000123", "FD dispatched", "FD there", "FD here 14:05 · #26-000123"
    (cleared: at, when); "" for none."""
    if fd == "cleared":
        shown = "FD here" + (f" {datetime.fromtimestamp(float(at), tz).strftime('%H:%M')}" if at else "")
    else:
        shown = FD_SHOWN.get(fd or "", "")
    return f"{shown} · #{number}" if shown and number else shown


def compose(ev, pack_label: str, cat: dict, public_url: str = "", tz=None, doubtful: bool = False,
            number: str = "", fd_at=None) -> dict:
    """{title, body, tags, priority, click} for the event (never the transcript). number: the incident it is linked
    to (an FYI's "FD on scene · #<number>"); fd_at: when the FD cleared it ("FD here HH:MM")."""
    town = ev.town or ""
    fd = fd_text(getattr(ev, "fd", ""), number, fd_at, tz)
    title = " · ".join(x for x in ("FYI" if getattr(ev, "fd", "") in FYI else "", pack_label, category_label(cat, pack_label, ev.category), town) if x)
    where = where_text(ev, doubtful)
    when = datetime.fromtimestamp(ev.first_at, tz).strftime("%H:%M") if ev.first_at else ""
    body = " · ".join(x for x in (where or "location not heard", when, ev.level, fd) if x)
    tags = [t for t in (cat.get("tag") or "", DOMAIN_TAG.get(ev.domain, "")) if t]
    click = ""
    if public_url and ev.id is not None:
        view = "police" if ev.domain == "police" else "all"
        click = f"{public_url.rstrip('/')}/radio?view={view}&intel={quote(str(ev.id))}"
    return {"title": title, "body": body, "tags": tags, "priority": int(ev.priority), "click": click}


def scrub_label(label: str, forbidden=()) -> str:
    """The same checks on a place's label alone (before it becomes an event's place)."""
    return scrub({"title": "", "body": label or ""}, forbidden)


def scrub(msg: dict, forbidden=()) -> str:
    """"" when the composed push is clean, else why not (a category only, never the text)."""
    text = f"{msg.get('title', '')}\n{msg.get('body', '')}"
    text = OUR_NUMBER.sub("", text)        # our own incident's number (an FYI's "#26-000123"): never a radio ID
    low = text.lower()
    for f in forbidden:
        f = str(f or "").strip()
        # a number alone is caught by RADIO_ID (6+ digits); a short one may be the house number itself
        if len(f) >= 3 and re.search(r"[A-Za-z]", f) and re.search(rf"(?<![a-z0-9]){re.escape(f.lower())}(?![a-z0-9])", low):
            return "talker"
    if RADIO_ID.search(text):
        return "radio_id"
    if SPACED_PLATE.search(text):
        return "plate"
    if DATE.search(text):
        return "date"
    if PLATE_LIKE.search(text):
        return "plate"
    return ""
