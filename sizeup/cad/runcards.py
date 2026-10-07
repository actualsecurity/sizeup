"""Run cards: event type -> the units to send, in order.

A card is keyed by a TYPE ("STRU") or a TYPE/SUBTYPE ("ALARM/COMM"); for an event the
subtype-specific card wins over the type-only one. A slot is either a kind with a count
({"kind": "engine", "count": 3}; "ladder|tower" takes either) or one named unit ({"unit": "2398"}).
The recommendation walks the slots in order and takes the next free unit for each: board order,
regular apparatus before spares, skipping units that can't be dispatched, are committed, or are
already on this event. Another department's units (mutual_aid in the data file) are only sent by
name, never picked by kind. A slot it can't fill is reported ("no ladder/tower available"), never
filled with something else.

The default cards come from data/cad_lists.json (run_cards). They are a stand-in so the console
works on day one, NOT PCFD policy: the page says so until each card has been edited.
"""

from __future__ import annotations

from sizeup.cad.lists import key
from sizeup.cad.store import CadError, Store

MAX_SLOTS, MAX_COUNT = 20, 10


def seed(store: Store, fill: bool = False) -> int:
    """The default cards, once (an empty table). Edited cards are never overwritten. fill: add
    the defaults whose key has no card yet (after a migration replaced the old defaults)."""
    have = {r["type"] for r in store.q("SELECT type FROM run_cards")}
    if have and not fill:
        return 0
    cards = [c for c in store.lists.run_cards if c["type"] not in have]
    if not cards:
        return 0
    with store.tx() as db:
        base = db.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM run_cards").fetchone()[0]
        for k, c in enumerate(cards, base):
            cid = db.execute("INSERT INTO run_cards (type, spoken, is_default, sort) VALUES (?, '', 1, ?)",
                             (c["type"], k)).lastrowid
            _write_slots(db, cid, c["slots"])
    return len(cards)


def _write_slots(db, cid: int, slots: list[dict]) -> None:
    db.execute("DELETE FROM run_card_slots WHERE card_id = ?", (cid,))
    for pos, s in enumerate(slots):
        db.execute("INSERT INTO run_card_slots (card_id, pos, kind, count, unit) VALUES (?, ?, ?, ?, ?)",
                   (cid, pos, s.get("kind", ""), int(s.get("count", 1)), s.get("unit", "")))


def cards(store: Store) -> list[dict]:
    out = store.q("SELECT * FROM run_cards ORDER BY sort, id")
    slots = store.q("SELECT * FROM run_card_slots ORDER BY card_id, pos")
    L = store.lists
    for c in out:
        c["is_default"] = bool(c["is_default"])
        typ, _, sub = c["type"].partition("/")
        c["label"] = L.type_label(typ, sub)
        c["phrase"] = c["spoken"] or L.phrase(typ, sub)      # what the dispatch will say
        c["slots"] = [({"unit": s["unit"]} if s["unit"] else {"kind": s["kind"], "count": s["count"]})
                      for s in slots if s["card_id"] == c["id"]]
    return out


def card(store: Store, k: str) -> dict | None:
    return next((c for c in cards(store) if c["type"] == k), None)


def resolve(store: Store, typ: str, subtype: str = "") -> dict | None:
    """The card for an event: TYPE/SUBTYPE if there is one, else TYPE, else none."""
    by = {c["type"]: c for c in cards(store)}
    return (by.get(key(typ, subtype)) if subtype else None) or by.get(typ)


def validate_slots(store: Store, slots) -> list[dict]:
    if not isinstance(slots, list) or not slots or len(slots) > MAX_SLOTS:
        raise CadError(f"slots: a list of 1 to {MAX_SLOTS}")
    names = {u["name"] for u in store.units()}
    kinds = store.lists.kind_ids
    out = []
    for i, s in enumerate(slots):
        if not isinstance(s, dict):
            raise CadError(f"slots[{i}]: an object")
        if s.get("unit"):
            if str(s["unit"]) not in names:
                raise CadError(f"slots[{i}]: no unit {s['unit']!r}")
            out.append({"unit": str(s["unit"])})
            continue
        ks = str(s.get("kind", "")).split("|")
        if not all(k in kinds for k in ks):
            raise CadError(f"slots[{i}].kind: one or more of {', '.join(kinds)} joined by |")
        try:
            n = int(s.get("count", 1))
        except (TypeError, ValueError):
            raise CadError(f"slots[{i}].count: a number") from None
        if not 1 <= n <= MAX_COUNT:
            raise CadError(f"slots[{i}].count: 1 to {MAX_COUNT}")
        out.append({"kind": "|".join(ks), "count": n})
    return out


def check_key(store: Store, k: str) -> str:
    try:
        typ, sub = store.lists.check_key((k or "").strip())
    except ValueError as e:
        raise CadError(str(e)) from None
    return key(typ, sub)


def save(store: Store, k: str, slots, spoken: str = "", rename_from: str = "") -> dict:
    """Add or edit one card (k = TYPE or TYPE/SUBTYPE). An edited card is no longer a default."""
    k = check_key(store, k)
    slots = validate_slots(store, slots)
    spoken = (spoken or "").strip()[:120]
    old = card(store, rename_from or k)
    if rename_from and old is None:
        raise CadError(f"no run card {rename_from!r}")
    if old is not None and old["type"] != k and card(store, k):
        raise CadError(f"a run card for {k} already exists")
    with store.tx() as db:
        if old is None:
            sort = (db.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM run_cards").fetchone()[0])
            cid = db.execute("INSERT INTO run_cards (type, spoken, is_default, sort) VALUES (?, ?, 0, ?)",
                             (k, spoken, sort)).lastrowid
        else:
            cid = old["id"]
            db.execute("UPDATE run_cards SET type = ?, spoken = ?, is_default = 0 WHERE id = ?", (k, spoken, cid))
        _write_slots(db, cid, slots)
    return card(store, k)


def delete(store: Store, k: str) -> None:
    c = card(store, k)
    if c is None:
        raise CadError(f"no run card {k!r}")
    with store.tx() as db:
        db.execute("DELETE FROM run_cards WHERE id = ?", (c["id"],))


def describe(slot: dict) -> str:
    if slot.get("unit"):
        return slot["unit"]
    return f"{slot['count']} × {slot['kind'].replace('|', '/')}"


def recommend(store: Store, typ: str, subtype: str = "", incident_id: int | None = None) -> dict:
    """{"units": [unit + "slot"], "unfilled": ["no ladder/tower available"], "card": key or None}.
    Units already on this event count toward their slots (a second alarm adds, it doesn't repeat).
    No card for the type: no units and a note, never an error (the operator adds units by hand)."""
    L = store.lists
    c = resolve(store, typ, subtype)
    units = store.units()
    on_here = [u for u in units if incident_id is not None and u["incident_id"] == incident_id]
    if c is None:
        what = L.type_label(typ, subtype) or "this event"
        return {"card": None, "card_label": "", "units": [], "unfilled": [], "already": [u["name"] for u in on_here],
                "note": f"No run card for {what}: add units by hand"}
    free = [u for u in units if u["status"] in L.free and u["incident_id"] is None]
    free.sort(key=lambda u: (u["spare"], u["sort"]))   # regular apparatus first, spares last
    taken: set[int] = set()
    picks, unfilled = [], []
    for slot in c["slots"]:
        if slot.get("unit"):
            want = [u for u in units if u["name"] == slot["unit"]]
            need, match = 1, (lambda u, n=slot["unit"]: u["name"] == n)
        else:
            kinds = slot["kind"].split("|")
            need, match = slot["count"], (lambda u, ks=kinds: u["kind"] in ks and not u["mutual_aid"])
            want = None
        for u in on_here:                              # already there: fills the slot
            if need and u["id"] not in taken and match(u):
                taken.add(u["id"])
                need -= 1
        for u in free:
            if need and u["id"] not in taken and match(u):
                taken.add(u["id"])
                picks.append(dict(u, slot=describe(slot)))
                need -= 1
        if need:
            if slot.get("unit"):
                u = want[0] if want else None
                why = "not on the boards" if u is None else (
                    f"committed to {u['incident_number']}" if u["incident_id"] else
                    L.name("unit_statuses", u["status"]).lower())
                unfilled.append(f"{slot['unit']} not available ({why})")
            else:
                unfilled.append(f"no {slot['kind'].replace('|', '/')} available" if need == slot["count"]
                                else f"only {slot['count'] - need} of {slot['count']} {slot['kind'].replace('|', '/')} available")
    return {"card": c["type"], "card_label": c["label"], "units": picks, "unfilled": unfilled,
            "already": [u["name"] for u in on_here], "note": ""}


def from_page(incident_type: str, category: str, text: str) -> tuple[str, str, str]:
    """A 60 Control page's parsed type (sizeup/dispatch.py vocabulary) -> (TYPE, SUBTYPE, note).
    Only the obvious ones are mapped; where the page doesn't say (commercial or residential? ALS
    or BLS?) the subtype or type is left blank with a note for the operator, never guessed."""
    t = " " + " ".join((text or "").lower().replace(",", " ").replace(".", " ").split()) + " "
    has = lambda *ws: any(f" {w} " in t for w in ws)
    heard = f"page: {incident_type.lower()}" if incident_type and incident_type != "Unknown" else "page"
    if incident_type in ("Working fire", "Structure fire"):
        return "STRU", "", ""
    if incident_type == "Fire alarm":
        if has("residential", "residence"):
            return "ALARM", "RESD", ""
        if has("commercial", "commercial line"):
            return "ALARM", "COMM", ""
        return "ALARM", "", "commercial or residential not stated on the page"
    if incident_type == "CO alarm":
        if has("commercial"):
            return "ALARM", "CO-COMM", ""
        if has("residential", "residence"):
            return "ALARM", "CO-RESD", ""
        return "ALARM", "", "CO alarm: commercial or residential not stated on the page"
    if incident_type == "Gas leak":
        return "HAZARD", "GAS", ""
    if incident_type == "Wires down":
        return "HAZARD", "WIRES", ""
    if incident_type == "Hazmat":
        return "HAZARD", "HAZMAT", ""
    if incident_type == "Brush fire":
        return "BRUSH", "", ""
    if incident_type == "Car fire":
        return "MVF", "TRUCK" if has("truck fire") else ("CAR" if has("car fire", "auto fire") else ""), ""
    if incident_type in ("MVA", "Entrapment"):
        if has("entrapment", "pin job", "trapped", "pinned", "extrication"):
            return "MVA", "EXTRI", ""
        return "MVA", "", ""
    if incident_type == "Rescue":
        if has("water", "marine", "boat", "river", "harbor", "sound", "drowning"):
            return "RESCUE", "WATER", ""
        return "RESCUE", "", ""
    if incident_type in ("Smoke / odor", "Investigation"):
        return "INVEST", "", ""
    if incident_type == "Mutual aid":
        return "MA", "", ""
    if incident_type == "Lockout":
        return "SERVICE", "", ""
    if incident_type == "Water condition":
        return "SERVICE", "", "water condition (flooding / leak): pick the subtype"
    if incident_type == "Elevator":
        return "", "", "elevator: rescue or service not clear from the page"
    if category == "ems" or incident_type == "EMS assist":
        if has("als", "a l s", "advanced life support"):
            return "ALS", "", ""
        if has("bls", "b l s", "basic life support"):
            return "BLS", "", ""
        return "", "", f"EMS ({heard}): ALS or BLS not stated on the page"
    return "", "", f"type not recognised ({heard})"
