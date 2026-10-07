"""Run cards: call type -> the units to send, in order.

A slot is either a kind with a count ({"kind": "engine", "count": 3}; "ladder|tower" takes either)
or one named unit ({"unit": "2398 Duty Officer"}). The recommendation walks the slots in order and
takes the next free unit for each: roster order, regular apparatus before spares, skipping units
that are out of service, busy, or already on this incident. A slot it can't fill is reported
("no ladder/tower available"), never filled with something else.

DEFAULT_CARDS is a stand-in so the console works on day one. It is NOT PCFD policy: the page
says so until each card has been edited.
"""

from __future__ import annotations

from sizeup.cad.store import FREE, KINDS, CadError, Store, label

# (type, spoken in the dispatch, slots)
DEFAULT_CARDS: list[tuple[str, str, list[dict]]] = [
    ("Structure fire", "a structure fire",
     [{"kind": "engine", "count": 3}, {"kind": "ladder|tower", "count": 1}, {"kind": "rescue", "count": 1},
      {"unit": "2398 Duty Officer"}]),
    ("Fire alarm – commercial", "a commercial fire alarm",
     [{"kind": "engine", "count": 1}, {"kind": "ladder|tower", "count": 1}, {"unit": "2398 Duty Officer"}]),
    ("Fire alarm – residential", "a residential fire alarm", [{"kind": "engine", "count": 1}]),
    ("CO alarm", "a carbon monoxide alarm", [{"kind": "engine", "count": 1}]),
    ("Odor of gas inside", "an odor of gas inside", [{"kind": "engine", "count": 1}, {"kind": "ladder|tower", "count": 1}]),
    ("Outside fire / brush", "an outside fire", [{"kind": "engine", "count": 1}]),
    ("MVA", "a motor vehicle accident", [{"kind": "engine", "count": 1}, {"kind": "rescue", "count": 1}]),
    ("MVA with entrapment", "a motor vehicle accident with entrapment",
     [{"kind": "engine", "count": 2}, {"kind": "rescue", "count": 1}, {"kind": "ladder|tower", "count": 1},
      {"unit": "2398 Duty Officer"}]),
    ("Wires down", "wires down", [{"kind": "engine", "count": 1}]),
    ("Water / marine rescue", "a water rescue",
     [{"unit": "Marine 40"}, {"kind": "rescue", "count": 1}, {"kind": "engine", "count": 1}]),
    ("EMS assist", "an EMS assist", [{"kind": "engine", "count": 1}]),
    ("Mutual aid request", "a mutual aid request", [{"unit": "2398 Duty Officer"}, {"kind": "engine", "count": 1}]),
    ("Other", "a call for service", [{"kind": "engine", "count": 1}]),
]
MAX_SLOTS, MAX_COUNT = 20, 10


def seed(store: Store) -> int:
    """The default cards, once (an empty table). Edited cards are never overwritten."""
    if store.one("SELECT 1 AS x FROM run_cards LIMIT 1"):
        return 0
    with store.tx() as db:
        for k, (typ, spoken, slots) in enumerate(DEFAULT_CARDS):
            cid = db.execute("INSERT INTO run_cards (type, spoken, is_default, sort) VALUES (?, ?, 1, ?)",
                             (typ, spoken, k)).lastrowid
            _write_slots(db, cid, slots)
    return len(DEFAULT_CARDS)


def _write_slots(db, cid: int, slots: list[dict]) -> None:
    db.execute("DELETE FROM run_card_slots WHERE card_id = ?", (cid,))
    for pos, s in enumerate(slots):
        db.execute("INSERT INTO run_card_slots (card_id, pos, kind, count, unit) VALUES (?, ?, ?, ?, ?)",
                   (cid, pos, s.get("kind", ""), int(s.get("count", 1)), s.get("unit", "")))


def cards(store: Store) -> list[dict]:
    out = store.q("SELECT * FROM run_cards ORDER BY sort, id")
    slots = store.q("SELECT * FROM run_card_slots ORDER BY card_id, pos")
    for c in out:
        c["is_default"] = bool(c["is_default"])
        c["slots"] = [({"unit": s["unit"]} if s["unit"] else {"kind": s["kind"], "count": s["count"]})
                      for s in slots if s["card_id"] == c["id"]]
    return out


def card(store: Store, typ: str) -> dict | None:
    return next((c for c in cards(store) if c["type"] == typ), None)


def validate_slots(store: Store, slots) -> list[dict]:
    if not isinstance(slots, list) or not slots or len(slots) > MAX_SLOTS:
        raise CadError(f"slots: a list of 1 to {MAX_SLOTS}")
    names = {u["name"] for u in store.units()}
    out = []
    for i, s in enumerate(slots):
        if not isinstance(s, dict):
            raise CadError(f"slots[{i}]: an object")
        if s.get("unit"):
            if str(s["unit"]) not in names:
                raise CadError(f"slots[{i}]: no unit named {s['unit']!r}")
            out.append({"unit": str(s["unit"])})
            continue
        kinds = str(s.get("kind", "")).split("|")
        if not all(k in KINDS for k in kinds):
            raise CadError(f"slots[{i}].kind: one or more of {', '.join(KINDS)} joined by |")
        try:
            n = int(s.get("count", 1))
        except (TypeError, ValueError):
            raise CadError(f"slots[{i}].count: a number") from None
        if not 1 <= n <= MAX_COUNT:
            raise CadError(f"slots[{i}].count: 1 to {MAX_COUNT}")
        out.append({"kind": "|".join(kinds), "count": n})
    return out


def save(store: Store, typ: str, slots, spoken: str = "", rename_from: str = "") -> dict:
    """Add or edit one card. An edited card is no longer a default."""
    typ = (typ or "").strip()
    if not typ or len(typ) > 60:
        raise CadError("type: 1 to 60 characters")
    slots = validate_slots(store, slots)
    spoken = (spoken or "").strip()[:120]
    old = card(store, rename_from or typ)
    if rename_from and old is None:
        raise CadError(f"no run card {rename_from!r}")
    if old is not None and old["type"] != typ and card(store, typ):
        raise CadError(f"a run card {typ!r} already exists")
    with store.tx() as db:
        if old is None:
            sort = (db.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM run_cards").fetchone()[0])
            cid = db.execute("INSERT INTO run_cards (type, spoken, is_default, sort) VALUES (?, ?, 0, ?)",
                             (typ, spoken, sort)).lastrowid
        else:
            cid = old["id"]
            db.execute("UPDATE run_cards SET type = ?, spoken = ?, is_default = 0 WHERE id = ?", (typ, spoken, cid))
        _write_slots(db, cid, slots)
    return card(store, typ)


def delete(store: Store, typ: str) -> None:
    c = card(store, typ)
    if c is None:
        raise CadError(f"no run card {typ!r}")
    with store.tx() as db:
        db.execute("DELETE FROM run_cards WHERE id = ?", (c["id"],))


def describe(slot: dict) -> str:
    if slot.get("unit"):
        return slot["unit"]
    kinds = slot["kind"].replace("|", "/")
    return f"{slot['count']} × {kinds}"


def recommend(store: Store, typ: str, incident_id: int | None = None) -> dict:
    """{"units": [unit + "slot"], "unfilled": ["no ladder/tower available"], "card": type}.
    Units already on this incident count toward their slots (a second alarm adds, it doesn't repeat)."""
    c = card(store, typ)
    if c is None:
        raise CadError(f"no run card for {typ!r}")
    units = store.units()
    on_here = [u for u in units if incident_id is not None and u["incident_id"] == incident_id]
    free = [u for u in units if u["status"] in FREE and u["incident_id"] is None]
    free.sort(key=lambda u: (u["spare"], u["sort"]))   # regular apparatus first, spares last
    taken: set[int] = set()
    picks, unfilled = [], []
    for slot in c["slots"]:
        if slot.get("unit"):
            want = [u for u in units if u["name"] == slot["unit"]]
            need, match = 1, (lambda u, n=slot["unit"]: u["name"] == n)
        else:
            kinds = slot["kind"].split("|")
            need, match = slot["count"], (lambda u, ks=kinds: u["kind"] in ks)
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
                why = "not in the roster" if u is None else (
                    "out of service" if u["status"] == "out_of_service" else
                    f"committed to {u['incident_number']}" if u["incident_id"] else label(u["status"]).lower())
                unfilled.append(f"{slot['unit']} not available ({why})")
            else:
                unfilled.append(f"no {slot['kind'].replace('|', '/')} available" if need == slot["count"]
                                else f"only {slot['count'] - need} of {slot['count']} {slot['kind'].replace('|', '/')} available")
    return {"card": c["type"], "units": picks, "unfilled": unfilled,
            "already": [u["name"] for u in on_here]}


def from_page(incident_type: str, category: str, text: str) -> str:
    """The run card for a 60 Control page's parsed type (sizeup/dispatch.py vocabulary). The
    operator can change it before accepting."""
    t = (text or "").lower()
    m = {"Working fire": "Structure fire", "Structure fire": "Structure fire", "Smoke / odor": "Structure fire",
         "CO alarm": "CO alarm", "Gas leak": "Odor of gas inside", "Brush fire": "Outside fire / brush",
         "Car fire": "Outside fire / brush", "Wires down": "Wires down", "Mutual aid": "Mutual aid request",
         "EMS assist": "EMS assist"}
    if incident_type == "Fire alarm":
        return "Fire alarm – residential" if "residential" in t else "Fire alarm – commercial"
    if incident_type in ("MVA", "Entrapment"):
        return "MVA with entrapment" if any(w in t for w in ("entrapment", "pin job", "trapped", "pinned")) else "MVA"
    if incident_type == "Rescue" and any(w in t for w in ("water", "marine", "boat", "river", "harbor", "sound")):
        return "Water / marine rescue"
    if incident_type in m:
        return m[incident_type]
    if category == "ems":              # MPDS chief complaints ("Breathing problem", "Fall", ...)
        return "EMS assist"
    return "Other"
