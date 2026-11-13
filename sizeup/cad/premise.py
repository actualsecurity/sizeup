"""Premise notes (#403 phase 2): what crews should know about a building, shown on every call there.

    premise_notes   one note per row, scoped to a building: its building key (sizeup/cad/addrkey.py; '' for a place
                    known only by its point: then matched within GROUP_M), the address as shown, the point, a category
                    (CATEGORIES), a priority (1 high, 2 normal, 3 low), the text, who wrote it and when, who changed it
                    last, a review-by date (shown as due once passed), an optional expiry, active (0: expired by a person)
    premise_acks    who saw a note on which incident, and when: an edit after the acknowledgement asks again

Surfacing: an open incident's notes ride on the state push (Cad.state: "premise" on each open incident), so they show
at the top of the incident card as soon as its address is keyed (a new address, a moved one). Ranked the same way
everywhere (rank()): priority, then how much the category matters to this call type (RELEVANCE), then the newest.
Every create, edit, expiry and restore is in the Settings audit log (section "premise"); the API adds the person's
activity line.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime

from sizeup.cad import addrkey
from sizeup.cad.settings import Editor
from sizeup.cad.store import TZ, CadError

CATEGORIES = {"hazard": "Hazard", "access": "Access/Knox", "medical": "Medical/O2", "animals": "Animals",
              "hoarding": "Hoarding", "other": "Other"}
PRIORITIES = {1: "High", 2: "Normal", 3: "Low"}
# The call type -> the categories in the order they matter to it; a type not here: CATEGORIES' order.
RELEVANCE = {
    "STRU": ("hazard", "hoarding", "access", "animals", "medical", "other"),
    "MVF": ("hazard", "access", "other", "animals", "hoarding", "medical"),
    "BRUSH": ("hazard", "access", "animals", "other", "hoarding", "medical"),
    "ALARM": ("access", "hazard", "animals", "hoarding", "medical", "other"),
    "HAZARD": ("hazard", "access", "medical", "animals", "hoarding", "other"),
    "INVEST": ("hazard", "access", "hoarding", "animals", "medical", "other"),
    "ALS": ("medical", "access", "animals", "hazard", "hoarding", "other"),
    "BLS": ("medical", "access", "animals", "hazard", "hoarding", "other"),
    "RESCUE": ("access", "hazard", "medical", "animals", "hoarding", "other"),
}
# Who may see a category's notes: category -> the least standing (Settings.standing's "may": viewer < dispatcher <
# admin). Not listed: everyone signed in. Set in Settings > System > Address notes (sizeup/cad/settings.py
# premise_visibility, admins, audited; Cad.premise_visible), this is the default before any edit. A restricted category
# never rides the state push (it goes to everyone); the card gets it from History here (GET, checked per person).
VISIBLE_TO: dict[str, str] = {}
STANDING = {"viewer": 0, "dispatcher": 1, "admin": 2}
TEXT_MAX = 500
SECTION = "premise"            # the Settings audit log's section
FIELDS = ("category", "priority", "text", "review_at", "expires_at", "active")

SCHEMA = """
CREATE TABLE IF NOT EXISTS premise_notes (
  id INTEGER PRIMARY KEY,
  bldg_key TEXT NOT NULL DEFAULT '',    -- the building (addrkey.py); '' = by its point
  addr_key TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',       -- the address as shown
  lat REAL, lon REAL,
  category TEXT NOT NULL,               -- hazard | access | medical | animals | hoarding | other
  priority INTEGER NOT NULL DEFAULT 2,  -- 1 high, 2 normal, 3 low
  text TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  updated_by TEXT NOT NULL DEFAULT '',
  review_at REAL,                       -- review by: shown as due once passed
  expires_at REAL,                      -- not shown from then on
  active INTEGER NOT NULL DEFAULT 1,    -- 0: expired by a person
  incident_id INTEGER                   -- saved from this incident
);
CREATE INDEX IF NOT EXISTS premise_bldg ON premise_notes(bldg_key) WHERE active = 1;
CREATE INDEX IF NOT EXISTS premise_point ON premise_notes(lat, lon) WHERE active = 1 AND lat IS NOT NULL;
CREATE TABLE IF NOT EXISTS premise_acks (
  note_id INTEGER NOT NULL REFERENCES premise_notes(id),
  incident_id INTEGER NOT NULL,
  at REAL NOT NULL,
  operator TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (note_id, incident_id)
);
CREATE INDEX IF NOT EXISTS premise_acks_incident ON premise_acks(incident_id);
"""


def rank(notes: list[dict], call_type: str = "") -> list[dict]:
    """Priority, then the category's relevance to the call type, then the newest (then the id: deterministic)."""
    order = RELEVANCE.get(call_type) or tuple(CATEGORIES)
    pos = {c: i for i, c in enumerate(order)}
    return sorted(notes, key=lambda n: (n["priority"], pos.get(n["category"], len(pos)), -n["updated_at"], -n["id"]))


def may_see(category: str, may: str, visible: dict | None = None) -> bool:
    """Whether someone of this standing ("admin" | "dispatcher" | "viewer") may see a category's notes (visible: the
    setting, else VISIBLE_TO)."""
    need = (VISIBLE_TO if visible is None else visible).get(category)
    return need is None or STANDING.get(may, 0) >= STANDING.get(need, 0)


def who(operator: str) -> str:
    """'Name <email>' -> 'Name'."""
    return re.sub(r"\s*<[^<>]*>\s*$", "", operator or "").strip() or (operator or "")


class Premise:
    """One per Cad (sizeup/cad/service.py)."""

    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        self.gen = 0                       # bumped on every write: the notes cache
        self._cache: dict = {}
        self._lock = threading.Lock()
        with self.store.lock:
            if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'incidents'").fetchone():
                self.store.db.executescript(SCHEMA)

    # -- reading ----------------------------------------------------------------------------------------------------

    def visible(self) -> dict:
        """Who may see each category now (the setting; VISIBLE_TO until it is edited)."""
        v = getattr(self.cad, "premise_visible", None)
        return VISIBLE_TO if v is None else v

    def can_see(self, n: dict, may: str) -> bool:
        return may_see(n["category"], may, self.visible())

    def _at(self, bldg_key: str, lat, lon) -> list[dict]:
        """The active notes at a building (cached until the next write): its key's; and, when the call or the note has
        no building key (known only by its point, or a place), the notes whose point is within GROUP_M of the call's.
        Two keyed buildings never share notes, however close."""
        if not bldg_key and lat is None:
            return []
        ck = (bldg_key, round(lat, 5) if lat is not None else None, round(lon, 5) if lon is not None else None)
        with self._lock:
            hit = self._cache.get(ck)
            if hit is not None and hit[0] == self.gen:
                return hit[1]
            gen = self.gen
        rows = self.store.q("SELECT * FROM premise_notes WHERE bldg_key = ? AND active = 1", (bldg_key,)) if bldg_key else []
        if lat is not None and lon is not None:
            a, b, c, d = addrkey.bbox(lat, lon)
            seen = {r["id"] for r in rows}
            rows += [r for r in self.store.q("SELECT * FROM premise_notes WHERE active = 1 AND lat IS NOT NULL AND lat BETWEEN ? AND ? "
                                             "AND lon BETWEEN ? AND ?", (a, b, c, d))
                     if r["id"] not in seen and (not bldg_key or not r["bldg_key"])
                     and addrkey.meters(lat, lon, r["lat"], r["lon"]) <= addrkey.GROUP_M]
        with self._lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[ck] = (gen, rows)
        return rows

    def shown(self, n: dict, now: float) -> dict:
        out = {k: n[k] for k in ("id", "label", "category", "priority", "text", "author", "created_at", "updated_at",
                                 "updated_by", "review_at", "expires_at", "incident_id")}
        out["active"] = bool(n["active"]) and not (n["expires_at"] is not None and n["expires_at"] <= now)
        out["review_due"] = n["review_at"] is not None and n["review_at"] <= now
        out["author_name"] = who(n["author"])
        out["restricted"] = n["category"] in self.visible()    # not in the state push: the card asks (History here)
        return out

    def for_incident(self, inc: dict, may: str = "admin") -> list[dict]:
        """An incident's notes the viewer may see (may: their standing; the state push: "viewer", what everyone may),
        ranked, each with its acknowledgement on this incident (ack: {at, by} | None; an edit after it: None again)."""
        now = self.cad.clock()
        notes = [n for n in self._at(inc.get("bldg_key") or "", inc.get("lat"), inc.get("lon"))
                 if (n["expires_at"] is None or n["expires_at"] > now) and self.can_see(n, may)]
        if not notes:
            return []
        acks = {a["note_id"]: a for a in self.store.q("SELECT note_id, at, operator FROM premise_acks WHERE incident_id = ?",
                                                     (inc["id"],))}
        out = []
        for n in rank(notes, inc.get("type") or ""):
            s = self.shown(n, now)
            a = acks.get(n["id"])
            s["ack"] = {"at": a["at"], "by": who(a["operator"])} if a and a["at"] >= n["updated_at"] else None
            # shown by its point, not its building: the card says whose it is ("nearby" and the note's own address)
            s["nearby"] = not (inc.get("bldg_key") and n["bldg_key"] == inc.get("bldg_key"))
            out.append(s)
        return out

    def decorate(self, incs: list[dict]) -> None:
        """The state push: each open incident's notes ("premise"), so they show on the card unasked."""
        for inc in incs:
            if inc.get("status") in ("queued", "pending", "active") and (inc.get("bldg_key") or inc.get("lat") is not None):
                try:
                    inc["premise"] = self.for_incident(inc, "viewer")     # one push for everyone: what all may see
                except Exception as e:  # noqa: BLE001 - never the push
                    self.cad.log(f"CAD premise: {type(e).__name__}: {e}")

    def listing(self, q: dict, may: str = "admin") -> dict:
        """GET /api/cad/premise?where=&all=1: the Search page's Notes (active ones, all=1: expired too), newest first; where:
        at that building (or the words in its address)."""
        def one(k: str, n: int = 120) -> str:
            return ((q.get(k) or [""])[0] or "").strip()[:n]
        conds, args = [], []
        if one("all") != "1":
            conds.append("active = 1")
        where = one("where")
        if where:
            k = self._key_text(where)
            like = "%" + where.lower().replace("%", "") + "%"
            conds.append("(lower(label) LIKE ?" + (" OR bldg_key = ?" if k.bldg_key else "") + ")")
            args += [like] + ([k.bldg_key] if k.bldg_key else [])
        rows = self.store.q("SELECT * FROM premise_notes" + (f" WHERE {' AND '.join(conds)}" if conds else "")
                            + " ORDER BY updated_at DESC, id DESC LIMIT 500", args)
        now = self.cad.clock()
        return {"notes": [self.shown(r, now) for r in rows if self.can_see(r, may)], "categories": CATEGORIES,
                "priorities": {str(k): v for k, v in PRIORITIES.items()}}

    # -- writing ----------------------------------------------------------------------------------------------------

    def _key_text(self, text: str) -> addrkey.Key:
        L = self.cad.lists
        return addrkey.key_text(text, L.geo_town_aliases, L.town_of, self.cad.geocoder)

    @staticmethod
    def _when(v, k: str) -> float | None:
        if v in (None, ""):
            return None
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        try:
            d = datetime.fromisoformat(str(v)[:40])
        except ValueError:
            raise CadError(f"{k}: a date (2026-12-31)") from None
        return (d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp()

    def _fields(self, body: dict, partial: bool) -> dict:
        f: dict = {}
        if not partial or "text" in body:
            t = " ".join(str(body.get("text") or "").split())
            if not t:
                raise CadError("text: what crews should know")
            if len(t) > TEXT_MAX:
                raise CadError(f"text: at most {TEXT_MAX} characters")
            f["text"] = t
        if not partial or "category" in body:
            c = str(body.get("category") or "other")
            if c not in CATEGORIES:
                raise CadError(f"category: one of {', '.join(CATEGORIES)}")
            f["category"] = c
        if not partial or "priority" in body:
            try:
                p = int(body.get("priority") or 2)
            except (TypeError, ValueError):
                p = 0
            if p not in PRIORITIES:
                raise CadError("priority: 1 (high), 2 (normal) or 3 (low)")
            f["priority"] = p
        for k in ("review_at", "expires_at"):
            if not partial or k in body:
                f[k] = self._when(body.get(k), k)
        return f

    def redact_audit(self, rows: list[dict], may: str) -> list[dict]:
        """The Settings audit log as this reader may see it: a premise note's rows (any written before its category was
        restricted too) reduced to its id, its category now and the names of what changed, when the reader may not see
        that category under the current setting. At read time: the rows themselves stay as written."""
        ids = {int(m.group(1)) for r in rows if r.get("section") == SECTION and (m := re.match(r"(\d+)", r.get("key") or ""))}
        if not ids:
            return rows
        cats = {r["id"]: r["category"] for r in self.store.q(
            f"SELECT id, category FROM premise_notes WHERE id IN ({','.join('?' * len(ids))})", list(ids))}

        def names(v):
            try:
                d = json.loads(v) if v else None
            except ValueError:
                return None
            return sorted(d) if isinstance(d, dict) else None
        out = []
        for r in rows:
            m = re.match(r"(\d+)", r.get("key") or "") if r.get("section") == SECTION else None
            cat = cats.get(int(m.group(1))) if m else None
            if m and (cat is None or not may_see(cat, may, self.visible())):
                def shape(v, cat=cat):
                    n = names(v)
                    return "" if n is None else json.dumps({"category": cat, "changed": n}, separators=(",", ":"))
                r = dict(r, key=m.group(1), old=shape(r.get("old")), new=shape(r.get("new")))
            out.append(r)
        return out

    def _audit(self, db, op: str, n: dict, old: dict | None, new: dict | None) -> None:
        """The Settings audit log (anyone who may read Settings): a note of a restricted category (or one moved in or out
        of one) is only its id, its category and the names of what changed: never its text or address."""
        vis = self.visible()
        cats = {n["category"], *((old or {}).get("category", ()),), *((new or {}).get("category", ()),)}
        if any(c in vis for c in cats if isinstance(c, str)):
            shape = lambda d, cat: None if d is None else {"category": cat, "changed": sorted(d)}   # noqa: E731
            Editor._audit(db, self.cad.clock(), op, SECTION, str(n["id"]),
                          shape(old, (old or {}).get("category", n["category"])),
                          shape(new, (new or {}).get("category", n["category"])))
            return
        Editor._audit(db, self.cad.clock(), op, SECTION, f"{n['id']} {n['label']}"[:200], old, new)

    def _bump(self) -> None:
        with self._lock:
            self.gen += 1

    def create(self, body: dict) -> dict:
        """POST /api/cad/premise {incident_id | where, text, category?, priority?, review_at?, expires_at?, operator}:
        from an incident (its building), or an address typed on the Search page."""
        f = self._fields(body, partial=False)
        op = str(body.get("operator") or "")[:320]
        iid = body.get("incident_id")
        if iid not in (None, ""):
            try:
                inc = self.store.incident(int(iid))
            except (TypeError, ValueError):
                raise CadError("incident_id: a number") from None
            self.cad.addrs.key_one(inc["id"])
            inc = self.store.incident(inc["id"])
            if not inc["bldg_key"] and inc["lat"] is None:
                raise CadError("Set the incident's location first")
            where = {"bldg_key": inc["bldg_key"], "addr_key": inc["addr_key"], "lat": inc["lat"], "lon": inc["lon"],
                     "label": (inc["addr_label"] or inc["address"] or inc["geo_label"])[:200], "incident_id": inc["id"]}
        else:
            where = self._where(body)
        now = self.cad.clock()
        with self.store.tx() as db:
            cur = db.execute("INSERT INTO premise_notes (bldg_key, addr_key, label, lat, lon, category, priority, text, author, "
                             "created_at, updated_at, updated_by, review_at, expires_at, active, incident_id) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                             (where["bldg_key"], where["addr_key"], where["label"], where["lat"], where["lon"], f["category"],
                              f["priority"], f["text"], op, now, now, op, f["review_at"], f["expires_at"], where["incident_id"]))
            n = dict(db.execute("SELECT * FROM premise_notes WHERE id = ?", (cur.lastrowid,)).fetchone())
            self._audit(db, op, n, None, {k: n[k] for k in FIELDS})
        self._bump()
        return {"ok": True, "note": self.shown(n, now)}

    def _where(self, body: dict) -> dict:
        """A typed address (and, picked on a map, its lat / lon) for a new note: only a key the calls there will have.
        The gazetteer must be sure of it (a misspelt street would be keyed apart from every call at the building), or a
        map pick gives its point (matched within GROUP_M); with no gazetteer here, the text as typed."""
        text = str(body.get("where") or "").strip()[:200]
        lat, lon = body.get("lat"), body.get("lon")
        try:
            point = (float(lat), float(lon)) if lat not in (None, "") and lon not in (None, "") else None
        except (TypeError, ValueError):
            raise CadError("lat, lon: numbers") from None
        if not text and point is None:
            raise CadError("where: a street address (123 Example Ave)")
        L = self.cad.lists
        inc = {"address": text} | ({"lat": point[0], "lon": point[1]} if point else {})
        k = addrkey.key_incident(inc, L.geo_town_aliases, L.town_of, self.cad.geocoder, L.event_town(inc))
        sure = k.how == "gazetteer" or (point is not None and k.how == "geo") or (self.cad.geocoder is None and k.how == "text")
        if not sure:
            raise CadError("Not an address the map knows: check the spelling, or add the note from a call there")
        if point is None and self.cad.geocoder is not None:      # the building's point: notes match within GROUP_M too
            p = addrkey.parse(text, L.geo_town_aliases)
            try:
                ms = self.cad.geocoder(f"{p.first} {p.street}" + (f", {p.town}" if p.town else "")) or [] if p else []
            except Exception:  # noqa: BLE001 - the note keeps its key, without a point
                ms = []
            m = next((m for m in ms if m.get("kind") == "address" and float(m.get("confidence") or 0) >= addrkey.SNAP_CONFIDENCE), None)
            if m:
                point = (m["lat"], m["lon"])
        return {"bldg_key": k.bldg_key, "addr_key": k.addr_key, "lat": point[0] if point else None,
                "lon": point[1] if point else None, "label": (k.label or text)[:200], "incident_id": None}

    def edit(self, nid: int, body: dict) -> dict:
        """POST /api/cad/premise/<id> {text?, category?, priority?, review_at?, expires_at?, action?: expire | restore}.
        A note of a category the person may not see is, for them, no note (body["_may"]: their standing, the API's)."""
        may = str(body.get("_may") or "admin")
        n = self.store.one("SELECT * FROM premise_notes WHERE id = ?", (nid,))
        if n is None or not self.can_see(n, may):
            raise CadError(f"no note {nid}")
        op = str(body.get("operator") or "")[:320]
        f = self._fields(body, partial=True)
        act = str(body.get("action") or "")
        if act == "expire":
            f["active"] = 0
        elif act == "restore":
            f["active"] = 1
        elif act:
            raise CadError("action: expire or restore")
        changes = {k: v for k, v in f.items() if n[k] != v}
        now = self.cad.clock()
        if not changes:
            return {"ok": True, "note": self.shown(n, now)}
        with self.store.tx() as db:
            db.execute(f"UPDATE premise_notes SET {', '.join(f'{k} = ?' for k in changes)}, updated_at = ?, updated_by = ? "
                       f"WHERE id = ?", (*changes.values(), now, op, nid))
            self._audit(db, op, n, {k: n[k] for k in changes}, changes)
            n = dict(db.execute("SELECT * FROM premise_notes WHERE id = ?", (nid,)).fetchone())
        self._bump()
        if not self.can_see(n, may):                   # moved into a category they may not see: not its text back
            return {"ok": True, "note": {"id": nid, "category": n["category"], "restricted": True}}
        return {"ok": True, "note": self.shown(n, now)}

    def ack(self, nid: int, body: dict) -> dict:
        """POST /api/cad/premise/<id>/ack {incident_id, operator}: seen on this call (only a note the person may see)."""
        n = self.store.one("SELECT * FROM premise_notes WHERE id = ?", (nid,))
        if n is None or not self.can_see(n, str(body.get("_may") or "admin")):
            raise CadError(f"no note {nid}")
        try:
            iid = int(body.get("incident_id"))
        except (TypeError, ValueError):
            raise CadError("incident_id: a number") from None
        inc = self.store.incident(iid)
        op = str(body.get("operator") or "")[:320]
        now = self.cad.clock()
        with self.store.tx() as db:
            db.execute("INSERT INTO premise_acks (note_id, incident_id, at, operator) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(note_id, incident_id) DO UPDATE SET at = excluded.at, operator = excluded.operator",
                       (nid, iid, now, op))
        return {"ok": True, "note_id": nid, "incident_id": iid, "number": inc["number"] or "",
                "ack": {"at": now, "by": who(op)}}
