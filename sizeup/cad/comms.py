"""Communications log: every transmission the monitor transcribed, tied to a rig and to CAD events.

    radio_traffic   one row per transmission (the monitor's clip id: its audio plays from
                    /api/audio/<id>.wav until the 12 h audio limit; the transcript is copied here
                    and kept), with the rig(s) the registry resolved and the raw radio ID(s)
    radio_links     transmission x event, and why:
                      unit        from a radio of a unit assigned to the event (dispatched or
                                  attached, until it cleared) at the time of the transmission
                      channel     an UNIDENTIFIED radio (no ID decoded) on the tactical channel (FG1)
                                  while exactly one emergency event had units on it. A known radio
                                  (any decoded ID: a rig, or e.g. 9231 the South End mobile) links
                                  only to events its rig is on (user, 2026-10-07: "why did this one
                                  get added to this call? it wasn't dispatched to that call").
                      dispatcher  a 60 Control turn right before or after a linked unit's turn in the
                                  same P25 call on an incident talkgroup (Fire/EMS 15)
                      manual      put on the event by a person ("+ event" on the 60 Control feed);
                                  unit = the operator who did it (also in the event's timeline)

Links are made when the transcript arrives and never moved afterwards; a re-run of
speech-to-text updates the copied text.
"""

from __future__ import annotations

import re
import time
from datetime import datetime

from sizeup.cad.radios import is_dispatcher
from sizeup.cad.store import TZ

TURN = re.compile(r"^(?P<call>.+)-t(?P<k>\d+)$")
NOTES = "Notes"          # the /comms "channel" of event notes tagged with a unit
KEEP_UNLINKED_S = 90 * 24 * 3600     # transmissions on no event: kept 90 days (linked ones: always)
MAX_ROWS = 500


def split_call(tid: str, p25: bool) -> tuple[str, int | None]:
    """'307-1790954731.970_476112500.0-call_11-t2' -> (call id, 2); a one-talker call -> (its id, 0)."""
    if not p25:
        return "", None
    m = TURN.match(tid)
    return (m.group("call"), int(m.group("k"))) if m else (tid, 0)


class Comms:
    def __init__(self, store, tactical: tuple[str, ...] = ("FG1",), incident_tgs: tuple[int, ...] = (307, 333),
                 clock=time.time):
        self.store, self.clock = store, clock
        self.tactical, self.incident_tgs = set(tactical), set(incident_tgs)
        self.rev = 0            # bumped on every new or changed transmission (the /comms page polls it)
        self._drop_bad_channel_links()

    def _drop_bad_channel_links(self) -> None:
        """Links the old channel rule made: a known radio, or one transmission on several events."""
        try:
            with self.store.tx() as db:
                db.execute("DELETE FROM radio_links WHERE why = 'channel' AND traffic_id IN "
                           "(SELECT id FROM radio_traffic WHERE radio_id != '' OR rig != '')")
                db.execute("DELETE FROM radio_links WHERE why = 'channel' AND traffic_id IN "
                           "(SELECT traffic_id FROM radio_links WHERE why = 'channel' GROUP BY traffic_id HAVING COUNT(*) > 1)")
        except Exception:
            pass                # no comms tables yet (a fresh store migrates first)

    # -- writing --------------------------------------------------------------------------------

    def record(self, t: dict, replace: bool = True) -> list[int]:
        """One transmission: {id, start, end, source, talkgroup, unit (talker as heard), rig, radio_id,
        text, status, model}. Returns the event ids it is linked to. replace=False: skip one already
        logged (the startup backfill)."""
        tid = str(t["id"])
        p25 = t.get("talkgroup") is not None
        call_id, turn = split_call(tid, p25)
        talker = t.get("unit") or ""
        disp = int(p25 and bool(talker) and all(is_dispatcher(x) for x in talker.split(",")))
        now = self.clock()
        start = float(t["start"])
        rigs = [r.strip() for r in (t.get("rig") or "").split(",") if r.strip()]
        with self.store.tx() as db:
            have = db.execute("SELECT 1 FROM radio_traffic WHERE id = ?", (tid,)).fetchone()
            if have and not replace:
                return []
            vals = (call_id, turn, start, float(t.get("end") or start), t.get("source") or "", t.get("talkgroup"),
                    talker, t.get("radio_id") or "", ", ".join(rigs), disp, t.get("text") or "", t.get("status") or "",
                    t.get("model") or "", now)
            if have:
                db.execute("UPDATE radio_traffic SET call_id = ?, turn = ?, start = ?, end = ?, channel = ?, talkgroup = ?, "
                           "talker = ?, radio_id = ?, rig = ?, dispatcher = ?, text = ?, status = ?, model = ?, updated_at = ? "
                           "WHERE id = ?", (*vals, tid))
            else:
                db.execute("INSERT INTO radio_traffic (call_id, turn, start, end, channel, talkgroup, talker, radio_id, rig, "
                           "dispatcher, text, status, model, updated_at, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (*vals, tid))
            links: dict[int, tuple[str, str]] = {}
            for rig in rigs:
                for r in db.execute(self.ON_EVENT_BY_UNIT, (rig, start, start)).fetchall():
                    links.setdefault(r["incident_id"], ("unit", rig))
            known = bool(rigs) or bool(t.get("radio_id"))
            if not links and not known and (t.get("source") or "") in self.tactical:
                active = db.execute(self.ACTIVE_EVENTS, (start, start)).fetchall()
                if len(active) == 1:            # two calls on FG1: can't tell whose it is
                    links[active[0]["incident_id"]] = ("channel", "")
            for iid, (why, unit) in links.items():
                db.execute("INSERT OR IGNORE INTO radio_links (traffic_id, incident_id, why, unit) VALUES (?, ?, ?, ?)",
                           (tid, iid, why, unit))
            if p25 and turn is not None and t.get("talkgroup") in self.incident_tgs:
                self._dispatcher_links(db, tid, call_id, turn, bool(disp), links)
            self.rev += 1
            return sorted(r[0] for r in db.execute("SELECT incident_id FROM radio_links WHERE traffic_id = ?", (tid,)))

    # A unit's assignment covering time T: from when it was put on the event until it cleared.
    ON_EVENT_BY_UNIT = ("SELECT DISTINCT a.incident_id FROM assignments a JOIN units u ON u.id = a.unit_id "
                        "WHERE u.name = ? AND a.assigned_at IS NOT NULL AND a.assigned_at <= ? "
                        "AND (a.cleared_at IS NULL OR a.cleared_at >= ?)")
    # Emergency events with any unit on them at time T (their tactical channel is in use).
    ACTIVE_EVENTS = ("SELECT DISTINCT a.incident_id FROM assignments a JOIN incidents i ON i.id = a.incident_id "
                     "WHERE i.emergency = 1 AND a.assigned_at IS NOT NULL AND a.assigned_at <= ? "
                     "AND (a.cleared_at IS NULL OR a.cleared_at >= ?)")

    def _dispatcher_links(self, db, tid: str, call_id: str, turn: int, disp: bool, mine: dict) -> None:
        """60 Control answering (or calling) a linked unit: the turn right before or after it in the
        same call. Either side may be transcribed first, so both directions are checked."""
        sib = db.execute("SELECT id, turn, dispatcher FROM radio_traffic WHERE call_id = ? AND turn IN (?, ?)",
                         (call_id, turn - 1, turn + 1)).fetchall()
        for s in sib:
            if disp and not s["dispatcher"]:            # me: dispatcher; neighbour: a unit linked by unit
                for r in db.execute("SELECT incident_id, unit FROM radio_links WHERE traffic_id = ? AND why = 'unit'",
                                    (s["id"],)).fetchall():
                    db.execute("INSERT OR IGNORE INTO radio_links (traffic_id, incident_id, why, unit) VALUES (?, ?, 'dispatcher', ?)",
                               (tid, r["incident_id"], r["unit"]))
            elif not disp and s["dispatcher"]:          # me: a unit; neighbour: the dispatcher
                for iid, (why, unit) in mine.items():
                    if why == "unit":
                        db.execute("INSERT OR IGNORE INTO radio_links (traffic_id, incident_id, why, unit) VALUES (?, ?, 'dispatcher', ?)",
                                   (s["id"], iid, unit))

    def link(self, tid: str, iid: int, operator: str = "") -> dict:
        """A person puts a transmission on an event (why 'manual', unit = the operator), with a line in
        the event's timeline. One already on that event is left as it was."""
        with self.store.tx() as db:
            t = db.execute("SELECT * FROM radio_traffic WHERE id = ?", (tid,)).fetchone()
            if t is None:
                return {"linked": False, "error": "no such transmission"}
            old = db.execute("SELECT why FROM radio_links WHERE traffic_id = ? AND incident_id = ?", (tid, iid)).fetchone()
            if old is not None:
                return {"linked": False, "why": old["why"]}
            db.execute("INSERT INTO radio_links (traffic_id, incident_id, why, unit) VALUES (?, ?, 'manual', ?)",
                       (tid, iid, operator))
            who = t["rig"] or t["talker"] or "radio"
            said = (t["text"] or "").strip()
            when = datetime.fromtimestamp(t["start"], TZ).strftime("%H:%M:%S")
            self.store._event(db, iid, "radio", f"Radio traffic added: {when} {who} on {t['channel']}"
                              + (f": \u201c{said[:160]}{'…' if len(said) > 160 else ''}\u201d" if said else ""), operator)
            self.rev += 1
        return {"linked": True, "why": "manual"}

    def retext(self, tid: str, text: str, status: str, model: str = "") -> bool:
        with self.store.tx() as db:
            n = db.execute("UPDATE radio_traffic SET text = ?, status = ?, model = ?, updated_at = ? WHERE id = ?",
                           (text, status, model, self.clock(), tid)).rowcount
            if n:
                self.rev += 1
        return bool(n)

    def prune(self) -> int:
        cutoff = self.clock() - KEEP_UNLINKED_S
        with self.store.tx() as db:
            return db.execute("DELETE FROM radio_traffic WHERE start < ? AND id NOT IN (SELECT traffic_id FROM radio_links)",
                              (cutoff,)).rowcount

    # -- reading --------------------------------------------------------------------------------

    def for_incidents(self, ids: list[int], limit: int = 200) -> dict[int, list[dict]]:
        """Each event's radio traffic, newest first."""
        out: dict[int, list[dict]] = {i: [] for i in ids}
        if not ids:
            return out
        rows = self.store.q(f"SELECT t.*, l.incident_id, l.why, l.unit AS link_unit FROM radio_links l "
                            f"JOIN radio_traffic t ON t.id = l.traffic_id WHERE l.incident_id IN ({','.join('?' * len(ids))}) "
                            f"ORDER BY t.start DESC", ids)
        for r in rows:
            lst = out[r.pop("incident_id")]
            if len(lst) < limit:
                lst.append(r)
        return out

    def query(self, unit: str = "", channel: str = "", event: str = "", pcfd: list[str] | None = None,
              since: float | None = None, until: float | None = None, limit: int = 300, kind: str = "",
              text: str = "") -> list[dict]:
        """The CAD's Radio view: newest first, with the event numbers each transmission is linked to, and
        (kind '' or 'notes') the event notes tagged with a unit ("E63 stated that ..."), as kind 'note'
        rows on channel "Notes". pcfd: only these rigs (PCFD's own units). text: every word in the
        transcript / note (any case)."""
        limit = max(1, min(int(limit), MAX_ROWS))
        words = [w.lower() for w in text.split()][:8]
        notes = self._notes(unit, event, pcfd, since, until, limit, words) if kind in ("", "notes") and channel in ("", NOTES) else []
        radio = self._radio(unit, channel, event, pcfd, since, until, limit, words) if kind in ("", "radio") and channel != NOTES else []
        return sorted(radio + notes, key=lambda r: -r["start"])[:limit]

    def _notes(self, unit, event, pcfd, since, until, limit, words=()) -> list[dict]:
        where, args = ["e.kind = 'note'", "e.units != ''"], []
        for w in words:
            where.append("LOWER(e.text) LIKE ? ESCAPE '\\'")
            args.append(f"%{_like(w)}%")
        if unit:
            where.append("(',' || e.units || ',') LIKE ?")
            args.append(f"%,{unit},%")
        if event:
            where.append("(i.number = ? OR CAST(i.id AS TEXT) = ?)")
            args += [event, event]
        if pcfd is not None:
            if not pcfd:
                return []
            where.append("(" + " OR ".join(["(',' || e.units || ',') LIKE ?"] * len(pcfd)) + ")")
            args += [f"%,{u},%" for u in pcfd]
        if since is not None:
            where.append("e.at >= ?")
            args.append(since)
        if until is not None:
            where.append("e.at <= ?")
            args.append(until)
        rows = self.store.q("SELECT e.id, e.at, e.text, e.operator, e.units, i.id AS iid, i.number, i.type, i.subtype, i.address "
                            "FROM events e JOIN incidents i ON i.id = e.incident_id WHERE " + " AND ".join(where)
                            + " ORDER BY e.at DESC LIMIT ?", (*args, limit))
        return [{"kind": "note", "id": f"note-{r['id']}", "start": r["at"], "end": r["at"], "channel": NOTES,
                 "talkgroup": None, "talker": r["operator"], "radio_id": "", "rig": r["units"].replace(",", ", "),
                 "dispatcher": 0, "text": r["text"], "status": "done", "operator": r["operator"],
                 "events": [{"id": r["iid"], "number": r["number"], "type": r["type"], "subtype": r["subtype"],
                             "address": r["address"], "why": "note", "unit": r["units"]}]} for r in rows]

    def _radio(self, unit, channel, event, pcfd, since, until, limit, words=()) -> list[dict]:
        where, args = [], []
        for w in words:
            where.append("LOWER(t.text) LIKE ? ESCAPE '\\'")
            args.append(f"%{_like(w)}%")
        if unit:
            where.append("(',' || REPLACE(t.rig, ' ', '') || ',') LIKE ?")
            args.append(f"%,{unit},%")
        if channel:
            where.append("t.channel = ?")
            args.append(channel)
        if event:
            where.append("t.id IN (SELECT l.traffic_id FROM radio_links l JOIN incidents i ON i.id = l.incident_id "
                         "WHERE i.number = ? OR CAST(i.id AS TEXT) = ?)")
            args += [event, event]
        if pcfd is not None:
            if not pcfd:
                return []
            where.append("(" + " OR ".join(["(',' || REPLACE(t.rig, ' ', '') || ',') LIKE ?"] * len(pcfd)) + ")")
            args += [f"%,{u},%" for u in pcfd]
        if since is not None:
            where.append("t.start >= ?")
            args.append(since)
        if until is not None:
            where.append("t.start <= ?")
            args.append(until)
        rows = self.store.q("SELECT t.*, 'radio' AS kind FROM radio_traffic t" + (" WHERE " + " AND ".join(where) if where else "")
                            + " ORDER BY t.start DESC LIMIT ?", (*args, limit))
        if rows:
            ids = [r["id"] for r in rows]
            ev: dict[str, list[dict]] = {}
            for l in self.store.q(f"SELECT l.traffic_id, l.why, l.unit, i.id, i.number, i.type, i.subtype, i.address "
                                  f"FROM radio_links l JOIN incidents i ON i.id = l.incident_id "
                                  f"WHERE l.traffic_id IN ({','.join('?' * len(ids))}) ORDER BY i.id", ids):
                ev.setdefault(l.pop("traffic_id"), []).append(l)
            for r in rows:
                r["events"] = ev.get(r["id"], [])
        return rows

    def facets(self) -> dict:
        return {"channels": [r["channel"] for r in self.store.q(
                    "SELECT channel, MAX(start) AS m FROM radio_traffic GROUP BY channel ORDER BY m DESC")] + [NOTES],
                "rigs": sorted({x.strip() for r in self.store.q("SELECT DISTINCT rig FROM radio_traffic WHERE rig != ''")
                                for x in r["rig"].split(",") if x.strip()})}


def _like(w: str) -> str:
    """A word for LIKE: its own % and _ (and the escape) taken literally."""
    return w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
