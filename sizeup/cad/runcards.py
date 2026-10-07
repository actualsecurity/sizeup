"""Run cards: event type -> the units to send, in order.

A card is keyed by a TYPE ("STRU") or a TYPE/SUBTYPE ("ALARM/COMM"); for an event the
subtype-specific card wins over the type-only one. A slot is either a kind with a count
({"kind": "engine", "count": 3}; "ladder|tower" takes either) or one named unit ({"unit": "2398"}).
The recommendation walks the slots in order and takes the next free unit for each: board order,
regular apparatus before spares, skipping units that can't be dispatched, are committed, or are
already on this event. A unit named on the card is kept for its own slot (a kind slot doesn't
take it). Another department's units (mutual_aid in the data file: Rye's L25, E191) are only sent
by name, never picked by kind. A joint-response department's units (joint_response: Rye Brook's
2662, L30, E14, U42) are direct units: a kind slot takes them after PCFD's own units of that kind,
and first when the event is in their town (their call). A slot it can't fill is reported
("no ladder/tower available"), never filled with something else. Other agencies' units (EMS, PD,
utilities: response "agency") are never on a card, by kind or by name: they are attached to an
event when they show up.

Nearest first (user, 2026-10-07: "can we use the location information of where these apparatus are
normally to recommend certain ones based on proximity to the next call?"): given where each rig is
now (sizeup/cad/mapview.py locate(): its station, on its way home from a call it just cleared, or a
live AVL fix) and the event's map point, a kind slot takes the free units of that kind by estimated
arrival (sizeup/cad/eta.py) instead of board order. The policy order stays on top: the event's joint
town's units, then PCFD's, then other joint units; regular apparatus before spares. Units whose
place is unknown (no home station) come after those with one, in board order; equal estimates keep
board order (several engines share a house). With no stations set, no map point on the event, or
Settings > Dispatch "run card order", the result is exactly the board order. Each unit carries its
estimate and why ("~2 min est. · returning from 26-000004"); named units stay as named.

The default cards come from data/cad_lists.json (run_cards). They are a stand-in so the console
works on day one, NOT PCFD policy: the page says so until each card has been edited.
"""

from __future__ import annotations

from sizeup.cad import eta
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
    by_name = {u["name"]: u for u in store.units()}
    L = store.lists
    kinds = L.card_kinds
    out = []
    for i, s in enumerate(slots):
        if not isinstance(s, dict):
            raise CadError(f"slots[{i}]: an object")
        if s.get("unit"):
            if str(s["unit"]) not in by_name:
                raise CadError(f"slots[{i}]: no unit {s['unit']!r}")
            if by_name[str(s["unit"])]["response"] == "agency":
                raise CadError(f"slots[{i}]: {s['unit']} is another agency's unit: never on a run card "
                               "(attach it to the event when it shows up)")
            out.append({"unit": str(s["unit"])})
            continue
        ks = str(s.get("kind", "")).split("|")
        if not all(k in kinds for k in ks):
            raise CadError(f"slots[{i}].kind: one or more of {', '.join(kinds)} joined by | "
                           "(EMS, PD and agencies are never on a run card; code-only units and mutual-aid "
                           "requests only by name)")
        try:
            n = int(s.get("count", 1))
        except (TypeError, ValueError):
            raise CadError(f"slots[{i}].count: a number") from None
        if not 1 <= n <= MAX_COUNT:
            raise CadError(f"slots[{i}].count: 1 to {MAX_COUNT}")
        out.append({"kind": "|".join(ks), "count": n})
    return out


def check_key(store: Store, k: str) -> str:
    """TYPE or TYPE/SUBTYPE, a pair the type -> subtype map allows (no ALS/BOMB card)."""
    try:
        typ, sub = store.lists.check_key((k or "").strip())
    except ValueError as e:
        raise CadError(f"run card {(k or '').strip()}: {e}") from None
    return key(typ, sub)


def save(store: Store, k: str, slots, spoken: str = "", rename_from: str = "") -> dict:
    """Add or edit one card (k = TYPE or TYPE/SUBTYPE). An edited card is no longer a default. A card
    kept under a pair the map no longer allows can still have its units edited (not a new key)."""
    k = (k or "").strip()
    if (rename_from and rename_from != k) or card(store, k) is None:
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


def default_card(store: Store, k: str) -> dict | None:
    return next((c for c in store.lists.run_cards if c["type"] == k), None)


def reset(store: Store, k: str) -> dict:
    """One card back to the data file's default (is_default again)."""
    d = default_card(store, k)
    if d is None:
        raise CadError(f"no default run card for {k}: delete it instead")
    c = card(store, k)
    with store.tx() as db:
        if c is None:
            sort = db.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM run_cards").fetchone()[0]
            cid = db.execute("INSERT INTO run_cards (type, spoken, is_default, sort) VALUES (?, '', 1, ?)", (k, sort)).lastrowid
        else:
            cid = c["id"]
            db.execute("UPDATE run_cards SET spoken = '', is_default = 1 WHERE id = ?", (cid,))
        _write_slots(db, cid, d["slots"])
    return card(store, k)


def reset_all(store: Store) -> None:
    """Every card back to the defaults: added cards removed, edited ones restored."""
    with store.tx() as db:
        db.execute("DELETE FROM run_cards")
    seed(store)


def replace_all(store: Store, new: list[dict]) -> None:
    """The cards from a settings import (checked first by the caller)."""
    with store.tx() as db:
        db.execute("DELETE FROM run_cards")
        for k, c in enumerate(new):
            cid = db.execute("INSERT INTO run_cards (type, spoken, is_default, sort) VALUES (?, ?, ?, ?)",
                             (c["type"], c.get("spoken", ""), int(bool(c.get("is_default"))), k)).lastrowid
            _write_slots(db, cid, c["slots"])


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


def estimates(units: list[dict], where: dict | None, inc: dict | None, params: eta.Params | None = None) -> dict[int, dict]:
    """unit id -> {eta_s (None: unknown), eta ("~3 min est." or ''), reason} for the given units:
    from where each is now (mapview.locate) to the event's map point."""
    p = params or eta.Params()
    to = [inc["lon"], inc["lat"]] if inc and inc.get("lat") is not None and inc.get("lon") is not None else None
    out = {}
    for u in units:
        loc = (where or {}).get(u["name"])
        if loc is None:
            out[u["id"]] = {"eta_s": None, "eta": "", "reason": "station not set"}
        elif to is None:
            out[u["id"]] = {"eta_s": None, "eta": "", "reason": loc["label"]}
        else:
            s = eta.eta_s(loc["ll"], to, p, from_quarters=not loc["on_road"])
            out[u["id"]] = {"eta_s": round(s), "eta": eta.text(s), "reason": f"{eta.text(s)} · {loc['label']}"}
    return out


def recommend(store: Store, typ: str, subtype: str = "", incident_id: int | None = None,
              where: dict | None = None, params: eta.Params | None = None) -> dict:
    """{"units": [unit + "slot", "eta_s", "eta", "reason"], "unfilled": ["no ladder/tower available"],
    "card": key or None, "order": {by: nearest|card, note}, "others": {unit id: estimate} (the other free
    units with an estimate: hints for adding one by hand)}.
    Units already on this event count toward their slots (a second alarm adds, it doesn't repeat).
    No card for the type: no units and a note, never an error (the operator adds units by hand).
    where: unit code -> mapview.locate() (None: nobody's place is known: board order);
    params: the ETA settings (eta.Params; its order "card" keeps board order, estimates still shown)."""
    L = store.lists
    p = params or eta.Params()
    c = resolve(store, typ, subtype)
    units = store.units()
    on_here = [u for u in units if incident_id is not None and u["incident_id"] == incident_id]
    inc = store.incident(incident_id) if incident_id is not None else None
    free = [u for u in units if u["status"] in L.free and u["incident_id"] is None]
    est = estimates(free, where, inc, p)
    located = inc is not None and inc.get("lat") is not None and inc.get("lon") is not None
    nearest = p.order == "nearest" and located and where is not None
    order = {"by": "nearest" if nearest else "card",
             "note": "" if nearest else "run card order (Settings > Dispatch)" if p.order != "nearest"
             else "no map point on the event: run card order" if not located else ""}
    others = {u["id"]: est[u["id"]] for u in free if u["response"] != "agency" and est[u["id"]]["eta"]}   # the add list's hints
    if c is None:
        what = L.type_label(typ, subtype) or "this event"
        return {"card": None, "card_label": "", "units": [], "unfilled": [], "already": [u["name"] for u in on_here],
                "note": f"No run card for {what}: add units by hand", "joint_call": "", "order": order, "others": others}
    town = L.joint_call(inc) if inc is not None else ""

    def rank(u: dict) -> int:          # the event's joint town's units, then PCFD's, then other joint units
        jt = L.joint_town(u["name"])
        return 0 if town and jt == town else (1 if not jt else 2)

    def near(u: dict) -> tuple:        # nearest first; unknown places after, in board order
        s = est[u["id"]]["eta_s"] if nearest else None
        return (0, s) if s is not None else (1, 0)
    free.sort(key=lambda u: (rank(u), u["spare"], *near(u), u["sort"]))   # regular apparatus before spares
    named = {s["unit"] for s in c["slots"] if s.get("unit")}
    taken: set[int] = set()
    picks, unfilled = [], []
    for slot in c["slots"]:
        if slot.get("unit"):
            want = [u for u in units if u["name"] == slot["unit"]]
            need, match = 1, (lambda u, n=slot["unit"]: u["name"] == n)
        else:
            kinds = slot["kind"].split("|")
            need, match = slot["count"], (lambda u, ks=kinds: u["kind"] in ks and u["response"] == "fire"
                                          and u["name"] not in named)
            want = None
        for u in on_here:                              # already there: fills the slot
            if need and u["id"] not in taken and match(u):
                taken.add(u["id"])
                need -= 1
        for u in free:
            if need and u["id"] not in taken and match(u) and u["response"] != "agency":
                taken.add(u["id"])
                picks.append(dict(u, slot=describe(slot), **est[u["id"]]))
                need -= 1
        if need:
            if slot.get("unit"):
                u = want[0] if want else None
                why = "not on the boards" if u is None else "another agency's unit: attach it" if u["response"] == "agency" else (
                    f"committed to {u['incident_number']}" if u["incident_id"] else
                    L.name("unit_statuses", u["status"]).lower())
                unfilled.append(f"{slot['unit']} not available ({why})")
            else:
                unfilled.append(f"no {slot['kind'].replace('|', '/')} available" if need == slot["count"]
                                else f"only {slot['count'] - need} of {slot['count']} {slot['kind'].replace('|', '/')} available")
    return {"card": c["type"], "card_label": c["label"], "units": picks, "unfilled": unfilled,
            "already": [u["name"] for u in on_here], "note": "", "joint_call": town, "order": order,
            "others": {k: v for k, v in others.items() if k not in taken}}


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
