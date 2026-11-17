"""The v1 shapes of the CAD's objects (#560): a fixed list of fields per object, never the console's blob (whose
sections mirror panels and change with the UI, docs/mobile-api.md section 1.2). Additive within v1: a field may be
added, none removed or renamed. Pure functions over the dicts the CAD already builds; nothing here reads a database.

Left out on purpose, however the console shows them: the caller's name, address and phone, the operators' sign-in
emails (a name only), every radio ID, rig and talker beyond what a row's own alias says, and the console's draw hints.
Ids are strings (docs/mobile-api.md section 3.2), times unix seconds."""
from __future__ import annotations

import re

from sizeup import sourcekeys

from .rev import rev_of

EVENT_MAX, DISPATCH_MAX, RADIO_MAX = 300, 50, 100        # a detail's lists are bounded (the CAD keeps 300 events)
_EMAIL = re.compile(r"\s*<[^<>]*>\s*$")                   # "Casey <casey@example.org>" -> "Casey"


def name_of(operator) -> str:
    """An operator as the people see them: the name, never the sign-in email."""
    s = _EMAIL.sub("", str(operator or "")).strip()
    return "" if re.fullmatch(r"\S+@\S+", s) else s          # (a bare email is no name)


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
            "address": i.get("address") or "", "cross_street": i.get("cross_street") or "",
            "place": i.get("place") or "", "lat": i.get("lat"), "lon": i.get("lon"),
            "remarks": i.get("remarks") or "", "disposition": i.get("disposition") or "",
            "source": i.get("source") or "", "created_at": i.get("created_at"), "accepted_at": i.get("accepted_at"),
            "closed_at": i.get("closed_at"), "until_at": i.get("until_at"),
            "units": [_assignment(a) for a in i.get("assignments") or []]}


_CALLER = ("caller:", "caller address:", "phone:")          # the segments of an "Edited ..." line that name the caller


def event_text(kind: str, text: str) -> str:
    """The chronology line; an edit line loses the caller's name, address and phone (store.update writes them in)."""
    if kind != "edit" or not text.startswith("Edited "):
        return text
    parts = [p for p in text[len("Edited "):].split("; ") if not p.startswith(_CALLER)]
    return "Edited " + "; ".join(parts) if parts else "Edited caller details"


def _event(e: dict) -> dict:
    return {"id": sid(e.get("id")), "at": e.get("at"), "kind": e.get("kind") or "",
            "text": event_text(e.get("kind") or "", e.get("text") or ""),
            "operator": name_of(e.get("operator")), "unit_id": sid(e.get("unit_id"))}


def _dispatch(d: dict) -> dict:
    return {"id": sid(d.get("id")), "at": d.get("at"), "kind": d.get("kind") or "", "text": d.get("text") or "",
            "operator": name_of(d.get("operator")), "unit_ids": [sid(u) for u in d.get("unit_ids") or []]}


def _premise(n: dict) -> dict:
    return {"id": sid(n.get("id")), "category": n.get("category") or "", "priority": n.get("priority"),
            "text": n.get("text") or "", "label": n.get("label") or "", "review_due": bool(n.get("review_due")),
            "expires_at": n.get("expires_at"), "updated_at": n.get("updated_at"), "nearby": bool(n.get("nearby"))}


def _preplan(p: dict) -> dict:
    return {"id": sid(p.get("id")), "kind": p.get("kind") or "", "label": p.get("label") or "",
            "note": p.get("note") or "", "lat": p.get("lat"), "lon": p.get("lon"), "level": p.get("level"),
            "priority": p.get("priority"), "review_due": bool(p.get("review_due")), "nearby": bool(p.get("nearby"))}


def radio_row(r: dict) -> dict:
    """A radio-log row on a call: its words and channel, the talker only as its alias says (never the radio ID)."""
    return {"id": str(r.get("id")), "at": r.get("start"), "channel": r.get("channel") or "",
            "text": r.get("text") or "", "unit": r.get("unit") or "", "emergency": bool(r.get("emergency")),
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
        "type_note": full.get("type_note") or "", "place_address": full.get("place_address") or "",
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
            "created_at": a.get("created_at"), "what": a.get("what") or "", "who": a.get("who") or "",
            "channel": a.get("channel") or "", "text": a.get("text") or "", "emergency": bool(a.get("emergency")),
            "drill": bool(a.get("drill")), "source": a.get("source") or "", "subject": a.get("subject") or "",
            "incident_id": sid(a.get("incident_id")), "incident_number": a.get("incident_number"),
            "has_clip": bool(a.get("has_clip")), "quiet": bool(a.get("quiet")),
            "ack_by": name_of(a.get("ack_by")), "ack_at": a.get("ack_at"),
            "clear_by": name_of(a.get("clear_by")), "clear_at": a.get("clear_at")}
