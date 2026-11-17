"""Out of service is a person's to end (admin, 2026-11-16: "we want an OOS object/unit to not be automatically put back
into service by one of our automations like from a radio transmission that says I'm responding").

    the gate   sizeup/cad/store.py: every unit status write and every put-on-a-call (set_unit_status, set_unit_status_on,
               assign, return_to_incident) says who is writing: by=PERSON (a person's own action through the API: the
               board, a dispatch, a suggestion's ✓, an undo, this notice's "In service") or the automation's name
               (radio | county | page; the default AUTO). While the unit is out of service (any status with role
               "oos": Out of service, Unmanned) an automated write is refused (OosLocked) and nothing about the unit
               changes. Fail closed: a new automation that names nobody is refused too
    notice     the refusal is kept here instead: "E63 out of service: radio heard “responding”", one line in the unit
               log and, when it was about a call, one on that call's timeline (kind "auto"); shown under the unit's row
               of the board with "In service?" (dispatchers and admins: POST /api/cad/oos-notice/<id>/service | dismiss;
               ✓ puts it at its free status, Available)
    dedupe     one notice per unit and reason (radio:enroute, county:dispatched, page:dispatched ...) within NOTICE_S:
               a repeat only counts (count, last_at), with no new line. Only notices made since the unit's current
               status was set count (first_at >= units.status_at): a new out-of-service decision starts fresh
    decided    ✓ or ✕ ends every open notice of that unit. After ✕ the unit is quiet for NOTICE_S (while the call of
               the notice dismissed, if any, is open): a refusal then is only counted on the dismissed notice, no line,
               no chip
    shown      (admin, 2026-11-16) for NOTICE_S after its last repeat, only while the unit is still out of service and
               the call it was about (if any) is open. Then it lapses: nothing changes, the unit stays as it was; a lapsed
               notice can't be acted on
    pushed     never: in-app only (admin, 2026-11-16)
    agency     another agency's unit a person attached to a call while it was out of service stays on it: the feed's in
               service can't release it (refused, a notice), so an automatic close becomes "close?" for a person
               (fail-safe, by design)
    suggestion a status suggestion heard for an out-of-service unit (sizeup/cad/statushints.py) is never taken by
               automation; a person's ✓ asks first ("E62 is out of service: put it on the call anyway?", confirm: true
               on POST /api/cad/hint/<id>/accept); left alone, it expires and the unit stays as it was
    radio IDs  a transmission from a radio on an out-of-service rig is still that rig's (the radio log, the transcript,
               its suggestion): only its status is never changed. A radio a person moved to another unit (Settings >
               Radios) is that unit's from then on, and works as any other
"""

from __future__ import annotations

NOTICE_S = 300.0               # shown this long after its last repeat (while its call is open); one per unit and reason
OUTCOMES = ("open", "in_service", "dismissed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS oos_notices (
  id INTEGER PRIMARY KEY,
  unit_id INTEGER NOT NULL,
  unit TEXT NOT NULL,                   -- the board code then
  status TEXT NOT NULL,                 -- its out-of-service status then (OS, UNMANNED)
  source TEXT NOT NULL,                 -- the automation refused: radio | county | page | automation
  reason TEXT NOT NULL,                 -- the dedupe key: <source>:<what it tried> (radio:enroute)
  what TEXT NOT NULL,                   -- as said: radio heard “responding”
  wanted TEXT NOT NULL DEFAULT '',      -- the status it would have set ('' = put on a call)
  incident_id INTEGER,                  -- the call it was about (NULL: none)
  traffic_id TEXT NOT NULL DEFAULT '',  -- the transmission (radio_traffic.id), when it was the radio
  first_at REAL NOT NULL,
  last_at REAL NOT NULL,
  count INTEGER NOT NULL DEFAULT 1,     -- refusals folded into it (NOTICE_S)
  outcome TEXT NOT NULL DEFAULT 'open', -- open | in_service | dismissed
  decided_at REAL,
  operator TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS oos_notices_unit ON oos_notices(unit_id, reason, last_at)
"""


def cause(source: str, what: str, reason: str = "", traffic_id: str = "", incident_id: int | None = None) -> dict:
    """What an automation was doing, for the notice if it is refused: cause("radio", "radio heard “responding”",
    "enroute", tid)."""
    return {"source": source, "what": what, "reason": reason, "traffic_id": traffic_id or "", "incident_id": incident_id}


def record(store, db, u, by: str, c: dict | None, iid, wanted: str, now: float) -> tuple[int, bool]:
    """A refusal kept, in the caller's transaction (u: the units row, with status_at). -> (notice id, new). A repeat of
    the same unit and reason within NOTICE_S is folded into the open notice: no new line. Quiet after a ✕ (the module
    docstring): counted on the dismissed notice only."""
    c = dict(c or {})
    source = str(c.get("source") or by or "automation")
    L = store.lists
    what = str(c.get("what") or f"{source} would set {L.status_phrase(wanted) if wanted else 'it on a call'}")
    reason = f"{source}:{c.get('reason') or wanted or 'assign'}"
    iid = c.get("incident_id") or iid
    quiet = db.execute("SELECT n.id FROM oos_notices n LEFT JOIN incidents i ON i.id = n.incident_id WHERE n.unit_id = ? "
                       "AND n.outcome = 'dismissed' AND n.decided_at >= ? AND n.first_at >= ? "
                       "AND (n.incident_id IS NULL OR i.status IN ('queued', 'pending', 'active')) ORDER BY n.id DESC LIMIT 1",
                       (u["id"], now - NOTICE_S, u["status_at"])).fetchone()
    if quiet is not None:                                # a person said "keep it out" just now: counted, nothing shown
        db.execute("UPDATE oos_notices SET count = count + 1 WHERE id = ?", (quiet["id"],))
        return quiet["id"], False
    old = db.execute("SELECT id FROM oos_notices WHERE unit_id = ? AND reason = ? AND outcome = 'open' AND last_at >= ? "
                     "AND first_at >= ? ORDER BY id DESC LIMIT 1", (u["id"], reason, now - NOTICE_S, u["status_at"])).fetchone()
    if old is not None:
        db.execute("UPDATE oos_notices SET last_at = ?, count = count + 1, traffic_id = COALESCE(NULLIF(?, ''), traffic_id), "
                   "incident_id = COALESCE(?, incident_id), what = ? WHERE id = ?",
                   (now, c.get("traffic_id") or "", iid, what, old["id"]))
        return old["id"], False
    cur = db.execute("INSERT INTO oos_notices (unit_id, unit, status, source, reason, what, wanted, incident_id, traffic_id, "
                     "first_at, last_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (u["id"], u["name"], u["status"], source, reason, what, wanted, iid, c.get("traffic_id") or "", now, now))
    line = f"{u['name']} {L.status_phrase(u['status'], u['name'], u['board'])}: {what}, not changed"
    store._event(db, None, "auto", line, f"{source.capitalize()} · auto", u["id"], now)
    if iid:
        store._event(db, iid, "auto", f"{u['name']} {L.status_phrase(u['status'], u['name'], u['board'])}: {what}, "
                     "not put on this call", f"{source.capitalize()} · auto", u["id"], now)
    return cur.lastrowid, True


class OosNotices:
    """The notices for the console (Cad.state "oos_notices") and their one tap."""

    def __init__(self, cad):
        self.cad, self.store = cad, cad.store

    def _shown(self, now: float, nid: int | None = None) -> list[dict]:
        """Open notices not lapsed: seen in the last NOTICE_S, their call (if any) still open, their unit still out of
        service and made since its status was set (an older out-of-service spell's never come back); newest first."""
        L = self.store.lists
        rows = self.store.q("SELECT n.*, u.status AS now_status, i.number AS incident_number FROM oos_notices n "
                            "JOIN units u ON u.id = n.unit_id LEFT JOIN incidents i ON i.id = n.incident_id "
                            "WHERE n.outcome = 'open' AND n.last_at >= ? AND n.first_at >= u.status_at "
                            "AND (n.incident_id IS NULL OR i.status IN ('queued', 'pending', 'active'))"
                            + (" AND n.id = ?" if nid is not None else "") + " ORDER BY n.last_at DESC, n.id DESC",
                            (now - NOTICE_S, *([nid] if nid is not None else [])))
        return [r for r in rows if L.role(r["now_status"]) == "oos"]     # back in service by any means: nothing to ask

    def active(self, now: float | None = None) -> list[dict]:
        """The notices shown now (_shown), newest first: {id, unit, unit_id, status, status_name, source, what,
        incident_id, incident_number, traffic_id, first_at, last_at, count}. A read only (nothing written on a GET)."""
        now = self.store.clock() if now is None else now
        L = self.store.lists
        out = []
        for r in self._shown(now):
            out.append({"id": r["id"], "unit": r["unit"], "unit_id": r["unit_id"], "status": r["now_status"],
                        "status_name": L.status_phrase(r["now_status"]), "source": r["source"], "what": r["what"],
                        "incident_id": r["incident_id"], "incident_number": r["incident_number"] or "",
                        "traffic_id": r["traffic_id"], "first_at": r["first_at"], "last_at": r["last_at"], "count": r["count"]})
        return out

    def act(self, nid: int, action: str, operator: str = "") -> dict:
        """POST /api/cad/oos-notice/<id>/service | dismiss: "service" puts the unit in service (its free status, a
        person's write, in its history and the unit log with why); "dismiss" leaves it out of service (and quiet for
        NOTICE_S). Either ends every open notice of that unit."""
        from sizeup.cad.store import PERSON, CadError
        if action not in ("service", "dismiss"):
            raise CadError("action: service or dismiss")
        n = self.store.one("SELECT * FROM oos_notices WHERE id = ?", (nid,))
        if n is None:
            raise CadError(f"no notice {nid}")
        if n["outcome"] != "open":
            raise CadError(f"already {'in service' if n['outcome'] == 'in_service' else n['outcome']}")
        L = self.store.lists
        u = self.store.unit(n["unit_id"])
        if L.role(u["status"]) != "oos":
            raise CadError(f"{u['name']} is {L.status_phrase(u['status'])} already")
        if not self._shown(self.store.clock(), nid):
            raise CadError("this notice has lapsed: set the unit's status on the board")
        out = dict(u)
        if action == "service":
            out = self.store.set_unit_status(u["id"], L.free_for(u["name"], u["board"]), operator,
                                             why=f"In service from the notice ({n['what']})", by=PERSON)
        now = self.store.clock()
        with self.store.tx() as db:
            db.execute("UPDATE oos_notices SET outcome = ?, decided_at = ?, operator = ? WHERE unit_id = ? AND outcome = 'open'",
                       ("in_service" if action == "service" else "dismissed", now, operator, n["unit_id"]))
        return dict(out, notice=nid, action=action, what=n["what"])
