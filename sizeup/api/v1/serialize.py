"""The v1 shapes of the CAD's objects (#560): a fixed list of fields per object, never the console's blob (whose
sections mirror panels and change with the UI, docs/mobile-api.md section 1.2). Additive within v1: a field may be
added, none removed or renamed. Pure functions over the dicts the CAD already builds; nothing here reads a database.

What is NOT sent, however the console shows it:
  - the caller's name, address and phone: not the fields, and not the chronology lines that carry them. The
    chronology is fail-safe: an event's free text is sent only for the kinds in EVENT_TEXT_KINDS (an explicit
    allow-list); every other kind (system, feed, radio, prealert, one not known yet) goes as kind + time + operator
    name with an empty text. An "Edited ..." line that touches a caller field is dropped whole, as is any other line
    that names one;
  - the operators' sign-in emails (a name only), every radio ID, rig and talker beyond a row's own alias, and the
    console's draw hints.
What IS sent as written, because the people on the call read it (every one of these can contain a name, address or phone
number that a human or a transcript put there; the server does NOT filter them): an incident's `remarks`, `type_note`,
`place` and address; the dispatches' text; the operators' notes and the dispatch remarks in the chronology; `status`
events (a unit's radio transcript, "E60 on scene: <said>"); `tones` events (paging-feed lines of up to 2000 characters
from outside the CAD); an alarm's `text`, `what`, `who` and `subject`; the radio rows' `text` in a detail (transcripts);
a premise note's `text`; a pre-plan point's `note`. The CAD does not write the caller's details into these fields
itself, but a human or a transcript can. Every free text goes through scrub(): an operator's sign-in email never
leaves (any "<...>" part is dropped, anywhere in the text: a "<5 min>" in a note loses it,
deliberately; any other token holding an "@" becomes "an operator"). The radio-ID mask ("radio 41228", "rid
41228", ...) applies to an alarm's `what`/`text`/`subject`, the
`alarm`-kind chronology lines and the identity fields only (an alarm's `who`, a radio row's `unit`, which also lose any
standalone number of 4+ digits): a "radio 41228" in `status`/`tones` events, radio-row transcripts, notes, addresses and
remarks is NOT masked (they are sent as written). Not scrubbed at all: unit names, pre-plan `label`, `geo_label`,
`town`.
Ids are strings (docs/mobile-api.md section 3.2), times unix seconds."""
from __future__ import annotations

import re

from sizeup import sourcekeys

from .rev import rev_of

EVENT_MAX, DISPATCH_MAX, RADIO_MAX = 300, 50, 100        # a detail's lists are bounded (the CAD keeps 300 events)
_EMAIL = re.compile(r"\s*<[^<>]*>\s*$")                   # "Casey <casey@example.org>" -> "Casey"
_ANGLE = re.compile(r"\s*<[^>]*>")                        # any <...> part, anywhere
_ANGLE_OPEN = re.compile(r"\s*<[^>@]*@.*$")               # an unterminated one holding an "@": "Casey <a@x"
_TOKEN = re.compile(r"\S+")
# a talker's ID in an alarm's words: "radio 41228", "rid 41228", "Radio  41228", "radio-41228", "radio #41228"
_RADIO_ID = re.compile(r"\b(?:radio|rid|id|unit)\W*\d{3,}\b", re.I)
_BARE_ID = re.compile(r"\b\d{4,}\b")
UNIDENTIFIED = "an unidentified radio"


def _token(m: re.Match) -> str:
    t = m.group(0)
    # (a standalone " @ " is a place's: "40 Example Rd @ Sample Market")
    return "an operator" if "@" in t and t != "@" else t


def scrub(text, radio_ids: bool = False, identity: bool = False) -> str:
    """A free text with no operator sign-in email in it (the CAD writes "Name <email>", or the bare email of a person
    with no name, into event lines and alarm words). Any angle-bracket part goes (a "<5 min>" in a note too: deliberate,
    fail-safe), then every whitespace-delimited token holding an "@" (any email form, nested or not) becomes ONE
    "an operator"; a standalone "@" stays. radio_ids (an alarm's own words): "radio 41228", "rid 41228", "ID 41228" and
    the like become "an unidentified radio". identity (an identity field: an alarm's `who`, a radio row's `unit`): also
    any standalone number of 4 or more digits. Digits in addresses, remarks and transcripts are never touched."""
    out = _ANGLE_OPEN.sub("", _ANGLE.sub("", str(text or "")))
    out = _TOKEN.sub(_token, out)
    if radio_ids or identity:
        out = _RADIO_ID.sub(UNIDENTIFIED, out)
    return _BARE_ID.sub(UNIDENTIFIED, out) if identity else out


def name_of(operator) -> str:
    """An operator as the people see them: the name, never the sign-in email (see scrub: nothing with an "@" is left).
    A bare email is no name: "" ."""
    s = scrub(_EMAIL.sub("", str(operator or "")).strip())
    return "" if s == "an operator" else s


def sid(v) -> str | None:
    return None if v is None else str(v)


def with_rev(obj: dict) -> dict:
    """obj plus its `rev` (the hash of everything else in it)."""
    return {**obj, "rev": rev_of(obj)}


def _assignment(a: dict) -> dict:
    return {"unit_id": sid(a.get("unit_id")), "name": a.get("name") or "", "status": a.get("status") or "",
            "dispatched_at": a.get("dispatched_at"), "enroute_at": a.get("enroute_at"),
            "onscene_at": a.get("onscene_at"), "cleared_at": a.get("cleared_at")}


def incident_summary(i: dict) -> dict:
    """What a list or the snapshot shows of an incident (no rev yet)."""
    return {"id": sid(i["id"]), "number": i.get("number"), "status": i.get("status") or "", "type": i.get("type") or "",
            "subtype": i.get("subtype") or "", "emergency": bool(i.get("emergency")),
            "received_via": i.get("received_via") or "", "priority": i.get("priority"),
            "address": scrub(i.get("address")), "cross_street": scrub(i.get("cross_street")),
            "place": scrub(i.get("place")), "lat": i.get("lat"), "lon": i.get("lon"),
            "remarks": scrub(i.get("remarks")), "disposition": scrub(i.get("disposition")),
            "source": i.get("source") or "", "created_at": i.get("created_at"), "accepted_at": i.get("accepted_at"),
            "closed_at": i.get("closed_at"), "until_at": i.get("until_at"),
            "units": [_assignment(a) for a in i.get("assignments") or []]}


# Event kinds (store._event) whose text is the CAD's own wording or an operator's note: sent. Not listed, so never sent
# as text: "system" (its "Caller info: name · address · phone" line), "radio" (a row's words, possibly another P25
# system's), "feed" (a page or a feed's text, up to 2000 characters), "prealert", and any kind added later.
EVENT_TEXT_KINDS = frozenset({"created", "accepted", "dismissed", "note", "upgrade", "dispatch", "remarks", "tones",
                              "attach", "status", "closed", "reopened", "alarm", "auto"})
_CALLER = re.compile(r"caller|phone", re.I)       # "caller: ", "caller address: ", "phone: " (store.update's labels)


def event_text(kind: str, text: str) -> str:
    """The chronology line as the app may read it (see the module docstring)."""
    return scrub(_event_text(kind, text), kind == "alarm", identity=kind == "alarm")   # (CAD-worded lines)


def _event_text(kind: str, text: str) -> str:
    if kind == "edit":
        # store.update joins "label: value" with "; ", and a value may hold "; " itself: so a line that touches a caller
        # field is dropped whole, never split
        return "Edited caller details" if _CALLER.search(text) else text
    return text if kind in EVENT_TEXT_KINDS and not text.startswith("Caller info:") else ""


def _event(e: dict) -> dict:
    return {"id": sid(e.get("id")), "at": e.get("at"), "kind": e.get("kind") or "",
            "text": event_text(e.get("kind") or "", e.get("text") or ""),
            "operator": name_of(e.get("operator")), "unit_id": sid(e.get("unit_id"))}


def _dispatch(d: dict) -> dict:
    return {"id": sid(d.get("id")), "at": d.get("at"), "kind": d.get("kind") or "", "text": scrub(d.get("text")),
            "operator": name_of(d.get("operator")), "unit_ids": [sid(u) for u in d.get("unit_ids") or []]}


def _premise(n: dict) -> dict:
    return {"id": sid(n.get("id")), "category": n.get("category") or "", "priority": n.get("priority"),
            "text": scrub(n.get("text")), "label": scrub(n.get("label")), "review_due": bool(n.get("review_due")),
            "expires_at": n.get("expires_at"), "updated_at": n.get("updated_at"), "nearby": bool(n.get("nearby"))}


def _preplan(p: dict) -> dict:
    return {"id": sid(p.get("id")), "kind": p.get("kind") or "", "label": p.get("label") or "",
            "note": scrub(p.get("note")), "lat": p.get("lat"), "lon": p.get("lon"), "level": p.get("level"),
            "priority": p.get("priority"), "review_due": bool(p.get("review_due")), "nearby": bool(p.get("nearby"))}


def radio_row(r: dict) -> dict:
    """A radio-log row on a call: its words and channel, the talker only as its alias says (never the radio ID)."""
    return {"id": str(r.get("id")), "at": r.get("start"), "channel": r.get("channel") or "",
            "text": scrub(r.get("text")), "unit": scrub(r.get("unit"), identity=True),
            "emergency": bool(r.get("emergency")),
            "has_audio": bool(r.get("has_audio"))}


def radio_rows(rows: list[dict], sees_context: bool) -> list[dict]:
    """The call's radio rows the reader may see (sourcekeys.may_see_context: another P25 system's rows only for a
    signed-in person, the console's one gate), newest first, at most RADIO_MAX."""
    keep = [r for r in rows if sees_context or not sourcekeys.is_context(r)]
    return [radio_row(r) for r in keep[:RADIO_MAX]]


def incident_detail(full: dict, premise: list, preplan: list, radio: list) -> dict:
    """One incident with its chronology, dispatches and what the person's role may see on its building (no rev yet)."""
    out = incident_summary(full)
    out.update({
        "type_note": scrub(full.get("type_note")), "place_address": scrub(full.get("place_address")),
        "geo_label": full.get("geo_label") or "", "town": full.get("town") or "",
        "aircraft": ({"type": full.get("acft_type") or "", "nature": full.get("acft_nature") or "",
                      "eta_min": full.get("acft_eta"), "runway": full.get("acft_runway") or "",
                      "souls": full.get("acft_souls"), "level": full.get("acft_level"),
                      "fuel": full.get("acft_fuel") or "", "cargo": full.get("acft_cargo") or ""}
                     if full.get("acft_type") else None),
        "events": [_event(e) for e in (full.get("events") or [])[-EVENT_MAX:]],
        "dispatches": [_dispatch(d) for d in (full.get("dispatches") or [])[-DISPATCH_MAX:]],
        "premise": [_premise(n) for n in premise], "preplan": [_preplan(p) for p in preplan], "radio": radio})
    return out


def unit(u: dict) -> dict:
    """A unit (no rev yet). `notes` and the console's status sets stay out."""
    ids = u.get("incident_ids") or ([u["incident_id"]] if u.get("incident_id") else [])
    return {"id": sid(u["id"]), "name": u.get("name") or "", "kind": u.get("kind") or "",
            "title": u.get("title") or "", "station": u.get("station") or "", "department": u.get("department") or "",
            "board": u.get("board") or "", "response": u.get("response") or "", "status": u.get("status") or "",
            "status_at": u.get("status_at"), "out_of_service": u.get("out_of_service") or "",
            "spare": bool(u.get("spare")), "mutual_aid": bool(u.get("mutual_aid")),
            "incident_ids": [str(i) for i in ids], "incident_number": u.get("incident_number")}


def alarm(a: dict, state: str) -> dict:
    """An emergency alarm or notice (no rev yet). `who` is the label the console shows (never a separate radio ID).
    state: open | notice | recent."""
    return {"id": sid(a["id"]), "state": state, "level": a.get("level") or "", "at": a.get("at"),
            "created_at": a.get("created_at"), "what": scrub(a.get("what"), True),
            "who": scrub(a.get("who"), identity=True),
            "channel": a.get("channel") or "", "text": scrub(a.get("text"), True),
            "emergency": bool(a.get("emergency")),
            "drill": bool(a.get("drill")), "source": a.get("source") or "", "subject": scrub(a.get("subject"), True),
            "incident_id": sid(a.get("incident_id")), "incident_number": a.get("incident_number"),
            "has_clip": bool(a.get("has_clip")), "quiet": bool(a.get("quiet")),
            "ack_by": name_of(a.get("ack_by")), "ack_at": a.get("ack_at"),
            "clear_by": name_of(a.get("clear_by")), "clear_at": a.get("clear_at")}
