"""Dispatch modes (#311): who moves an external CAD feed's incident through the call, 60 Control or us. On the CAD of
record (the hub, or all-in-one), on top of the incident sources (sizeup/cad/sources.py, #297).

    mode       department-wide, in the header: "60control" (60 Control dispatching, the default) or "pcfd" (PCFD
               dispatching). Dispatchers and admins switch it (POST /api/cad/dispatch-mode); each switch is a Settings
               audit row (section "dispatch", key "dispatch_mode", who: the Access identity) and a log line. Kept in
               settings_overrides (section "dispatch_mode", key "mode", with who set it last; not set: the default)
    tracked    an incident the feed reported while in 60 Control mode (auto_incidents.tracked_at). One first reported in
               PCFD mode stays manual for good. A switch affects only what comes after it: every feed report is
               remembered as processed (auto_incidents.seen) in either mode, and automation applies only the change
               from the last report processed, so nothing reported while in PCFD mode is replayed after switching back
    60 Control (a tracked incident, while in 60 Control mode):
      accept   queued -> numbered at once when the feed lists one of our units or a page attached (and it has a type: a
               feed type nobody maps waits for a person). Never when it is "possibly the same as" an open accepted
               incident (sources.py): it stays queued for a person, said once on its timeline
      paged    a page attached to it (the paging decoder's report): "Paged 14:31:02" at the tone time (source radio)
      units    each unit the feed newly lists that is ours is dispatched at the feed's dispatched time: matched exactly
               against the board's codes and spoken names (spaces and dashes aside), or the data file's
               "feed_unit_aliases" {feed code: board code}; codes that match none are listed on the timeline, never
               dispatched. Never a unit out of service, on another open call (noted), or that was on this incident
               before (a cleared assignment stays cleared)
      statuses per unit, only for a unit on this incident now: the feed's unit_times (responding, on location, in
               service) move it, forward only against this assignment's own times and its status; the feed's
               incident times only when the feed lists one unit and it is ours (an incident time doesn't say which
               unit it was). The radio's status hints (sizeup/cad/statushints.py) for a unit on a tracked incident are
               taken at once (responding, on location, staging, back in service / quarters; never "clear", command or
               out of service, which stay suggestions). A unit on no call is put on one only by a "responding" said
               by its own identified radio or naming it (who: talker | named), and only when exactly one incident
               (tracked or not) is open and opened in the last ASSIGN_WINDOW_S, and that one is tracked
      close    the feed's in_service (or closed): closed COMPLETE when no unit is still on it; a unit a person put
               or kept on it, or one the feed gave no in-service time, keeps it open with "County in service: close?"
               on the timeline (one click closes it). Undoing a close puts back the units it released
      source   every automatic step is an auto_steps row with its source (county | radio) and its timeline entry; each
               (but accept) is undone with one click (POST /api/cad/incident/<id>/auto-undo {step}), which marks the
               unit (or the close) as a person's
    manual     a person's change wins: a unit a person dispatched, attached, set a status on, returned or had a radio
               suggestion accepted for, on that incident (manual_edits field "unit:<code>", #304's table), and an
               incident a person reopened ("close"), are left alone by automation from then on. A person can always
               work the call by hand in either mode
    PCFD       nothing automatic: feed incidents arrive queued (sources.py), as before
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime

import re
from pathlib import Path

from sizeup.cad.sources import FEED_SOURCE, PAGING
from sizeup.cad.store import OPEN, TZ, CadError

MODES = {"60control": "60 Control dispatching", "pcfd": "PCFD dispatching"}
SHORT = {"60control": "60 Control", "pcfd": "PCFD"}
DEFAULT = "60control"
SECTION, KEY = "dispatch_mode", "mode"
ASSIGN_WINDOW_S = 20 * 60.0
SOURCES = ("county", "radio")
RANK = {"dispatched": 1, "enroute": 2, "onscene": 3}
HINT_INTENTS = ("enroute", "onscene", "staging", "free", "quarters")
FEED_STATUS = {"responding": "enroute", "on_location": "onscene", "in_service": "free"}
COLS = {"dispatched": "dispatched_at", "enroute": "enroute_at", "onscene": "onscene_at", "free": "cleared_at"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_incidents (
  incident_id INTEGER PRIMARY KEY,
  tracked_at REAL,                      -- NULL: first reported in PCFD mode, manual for good
  seen TEXT NOT NULL DEFAULT '{}',      -- the feed's report as last processed (FeedEvent.as_dict)
  paged_at REAL,
  started_at REAL,                      -- automation first took it while pending / active (units listed before: new then)
  held TEXT NOT NULL DEFAULT ''         -- why it was left queued for a person (said once)
);
CREATE TABLE IF NOT EXISTS auto_steps (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL,
  at REAL NOT NULL,                     -- when it happened: the feed's time, the tone time, the transmission's start
  made_at REAL NOT NULL,
  kind TEXT NOT NULL,                   -- accept | paged | assign | status | close | close? (a suggestion)
  source TEXT NOT NULL,                 -- county | radio
  unit TEXT NOT NULL DEFAULT '',
  unit_id INTEGER,
  prev TEXT NOT NULL DEFAULT '',        -- the unit's status before
  status TEXT NOT NULL DEFAULT '',      -- the status it set
  col TEXT NOT NULL DEFAULT '',         -- the assignment time it stamped (enroute_at ...)
  ref TEXT NOT NULL DEFAULT '',         -- the feed's time key, the transmission (radio_traffic.id), a close's released units
  text TEXT NOT NULL,
  event_id INTEGER,                     -- its timeline entry
  undone_at REAL,
  undone_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS auto_steps_incident ON auto_steps(incident_id);
CREATE TABLE IF NOT EXISTS auto_possibly (     -- incidents sources.py noted "possibly the same" (both ways)
  incident_id INTEGER NOT NULL,
  other_id INTEGER NOT NULL,
  at REAL NOT NULL,
  PRIMARY KEY (incident_id, other_id)
);
"""


def _key(s: str) -> str:
    """A unit code compared as written: 'E-60' / 'e 60' -> 'E60'; 'Engine 60' -> 'ENGINE60' (never shortened)."""
    return re.sub(r"[\s-]+", "", str(s or "")).upper()




def _hm(t: float | None) -> str:
    return datetime.fromtimestamp(t, TZ).strftime("%H:%M:%S") if t else "—"


class AutoDispatch:
    def __init__(self, cad, clock=time.time, log=print):
        self.cad, self.store, self.clock, self.log = cad, cad.store, clock, log
        self._lock = threading.RLock()
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    # -- the mode --------------------------------------------------------------------------------------------------

    def _row(self) -> dict | None:
        return self.store.one("SELECT value, updated_at, operator FROM settings_overrides WHERE section = ? AND key = ?",
                              (SECTION, KEY))

    def mode(self) -> str:
        r = self._row()
        try:
            m = json.loads(r["value"]) if r else DEFAULT
        except ValueError:
            m = DEFAULT
        return m if m in MODES else DEFAULT

    def state(self) -> dict:
        r = self._row()
        m = self.mode()
        return {"mode": m, "label": MODES[m], "short": SHORT[m], "modes": [{"mode": k, "label": v, "short": SHORT[k]}
                                                                          for k, v in MODES.items()],
                "at": r["updated_at"] if r else None, "by": r["operator"] if r else ""}

    def set_mode(self, mode: str, operator: str = "") -> dict:
        """The header's switch: audited (Settings > Audit, section dispatch) and logged with who did it."""
        if mode not in MODES:
            raise CadError(f"mode: {' or '.join(MODES)}")
        with self._lock:
            return self._set_mode(mode, operator)

    def _set_mode(self, mode: str, operator: str) -> dict:
        old = self.mode()
        if mode != old:
            now = self.clock()
            with self.store.tx() as db:
                db.execute("INSERT INTO settings_overrides (section, key, value, updated_at, operator) VALUES (?, ?, ?, ?, ?) "
                               "ON CONFLICT(section, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                               "operator = excluded.operator", (SECTION, KEY, json.dumps(mode), now, operator))
                db.execute("INSERT INTO settings_audit (at, operator, section, key, old, new) VALUES (?, ?, 'dispatch', ?, ?, ?)",
                           (now, operator or "", SECTION, json.dumps(MODES[old]), json.dumps(MODES[mode])))
            self.log(f"CAD: dispatch mode {MODES[old]} -> {MODES[mode]}" + (f" by {operator}" if operator else ""))
        return {"ok": True, "changed": mode != old, **self.state()}

    # -- a person's changes win ------------------------------------------------------------------------------------

    def mark(self, iid: int | None, fields, operator: str = "") -> None:
        """A person changed these on the incident ("unit:E61", "close"): automation leaves them alone from now on."""
        fields = [f for f in dict.fromkeys(fields) if f]
        if not iid or not fields:
            return
        now = self.clock()
        with self._lock, self.store.tx() as db:      # never between automation's check and its write (L4)
            for f in fields:
                db.execute("INSERT INTO manual_edits (incident_id, field, at, operator) VALUES (?, ?, ?, ?) "
                           "ON CONFLICT(incident_id, field) DO UPDATE SET at = excluded.at, operator = excluded.operator",
                           (iid, f, now, operator))

    def mark_units(self, iid: int | None, names, operator: str = "") -> None:
        self.mark(iid, [f"unit:{n}" for n in names if n], operator)

    def _manual(self, iid: int, field: str) -> bool:
        return self.store.one("SELECT 1 AS x FROM manual_edits WHERE incident_id = ? AND field = ?", (iid, field)) is not None

    def _marked_at(self, iid: int, field: str) -> float | None:
        r = self.store.one("SELECT at FROM manual_edits WHERE incident_id = ? AND field = ?", (iid, field))
        return r["at"] if r else None

    # -- the feed ----------------------------------------------------------------------------------------------------

    def _tracking(self, iid: int) -> dict | None:
        return self.store.one("SELECT * FROM auto_incidents WHERE incident_id = ?", (iid,))

    def tracked(self, iid: int) -> bool:
        r = self._tracking(iid)
        return bool(r and r["tracked_at"] is not None)

    def _feed(self, iid: int) -> tuple[dict | None, str]:
        r = self.store.one("SELECT data, label FROM source_reports WHERE incident_id = ? AND source != ? AND merged_into = '' "
                           "ORDER BY rank, first_seen LIMIT 1", (iid, PAGING))
        return (json.loads(r["data"] or "{}"), r["label"] or "County feed") if r else (None, "")

    def on_feed(self, iid: int | None) -> None:
        """A feed's report applied to an incident (Cad.source_event): tracked or not, and in 60 Control mode the change
        since the last report processed, carried out. Never raises (the report stands whatever this does)."""
        if not iid:
            return
        first = False
        try:
            with self._lock:
                first = self._on_feed(iid)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: dispatch mode: {type(e).__name__}: {e}")
        if first:                                        # the radio log's backfill: after, outside the lock (M3)
            self.cad.radiolog.backfill_soon(iid)

    def _on_feed(self, iid: int) -> bool:
        """True: the first report on this incident."""
        data, label = self._feed(iid)
        if data is None:
            return False
        row = self._tracking(iid)
        auto = self.mode() == "60control"
        first = row is None
        if first:
            with self.store.tx() as db:
                db.execute("INSERT OR IGNORE INTO auto_incidents (incident_id, tracked_at, seen) VALUES (?, ?, '{}')",
                           (iid, self.clock() if auto else None))
            row = self._tracking(iid)
        if auto and row["tracked_at"] is not None:
            self._apply(iid, json.loads(row["seen"] or "{}"), data, label)
        with self.store.tx() as db:
            db.execute("UPDATE auto_incidents SET seen = ? WHERE incident_id = ?",
                       (json.dumps(data, separators=(",", ":")), iid))
        return first

    def on_page(self, iid: int | None) -> None:
        """A page attached to an incident a feed has: "paged" on a tracked one, in 60 Control mode."""
        if not iid:
            return
        try:
            with self._lock:
                if self.mode() == "60control" and self.tracked(iid):
                    inc = self.store.incident(iid)
                    if inc["status"] in ("pending", "active"):
                        self._paged(iid)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: dispatch mode: {type(e).__name__}: {e}")

    def _aliases(self) -> dict[str, str]:
        """The data file's "feed_unit_aliases": {feed code: board code} (optional)."""
        L = self.store.lists
        try:
            d = json.loads(Path(L.path).read_text()) if L.path else {}
        except (OSError, ValueError):
            d = {}
        a = d.get("feed_unit_aliases") or {}
        return {_key(k): str(v) for k, v in a.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(a, dict) else {}

    def _ours(self) -> dict[str, dict]:
        """A feed's unit code as written -> our board unit (the fire response: PCFD and the joint response): the board
        code or its spoken name, exactly (spaces, dashes and case aside), or an alias. Nothing looser (M1)."""
        units = [u for u in self.store.units() if u["response"] == "fire" and u["board"]]
        out: dict[str, dict] = {}
        for u in units:
            for k in (_key(u["name"]), _key(u["spoken"])):
                if k:
                    out.setdefault(k, u)
        by = {u["name"]: u for u in units}
        for k, code in self._aliases().items():
            if code in by:
                out[k] = by[code]
        return out

    def _held(self, iid: int, why: str, op: str) -> None:
        r = self._tracking(iid)
        if r is not None and r["held"] == why:
            return
        with self.store.tx() as db:
            db.execute("UPDATE auto_incidents SET held = ? WHERE incident_id = ?", (why, iid))
            self.store._event(db, iid, "auto", why, op)

    def possibly(self, a: int, b: int) -> None:
        """sources.py noted these two "possibly the same" (Sources.on_possibly)."""
        with self.store.tx() as db:
            for x, y in ((a, b), (b, a)):
                db.execute("INSERT OR IGNORE INTO auto_possibly (incident_id, other_id, at) VALUES (?, ?, ?)", (x, y, self.clock()))

    def _possibly_open(self, iid: int) -> dict | None:
        """An open accepted incident this one is possibly the same as (auto_possibly), else None."""
        return self.store.one("SELECT i.* FROM auto_possibly p JOIN incidents i ON i.id = p.other_id WHERE p.incident_id = ? "
                              "AND i.status IN ('pending', 'active') ORDER BY i.id LIMIT 1", (iid,))

    def _apply(self, iid: int, old: dict, new: dict, label: str) -> None:
        op = f"{label} · auto"
        inc = self.store.incident(iid)
        ours = self._ours()
        units = list(new.get("units") or [])
        mine = [n for n in units if _key(n) in ours]
        if inc["status"] == "queued":
            paged = self.store.one("SELECT 1 AS x FROM source_reports WHERE incident_id = ? AND source = ?", (iid, PAGING))
            same = self._possibly_open(iid)
            if same is not None:                         # a likely duplicate of a call already running: a person decides
                self._held(iid, f"Not accepted automatically: possibly the same as {same['number'] or '#' + str(same['id'])}",
                           op)
                return
            if not mine and not paged:
                return                                   # none of ours, no page: not our call (yet)
            if not inc["type"]:
                return                                   # a type nobody maps: a person accepts it, then it goes on
            self.store.accept(iid, op)
            self._step(iid, "accept", "county", f"Accepted ({label})", self.clock(), event_kind="accepted")
            inc = self.store.incident(iid)
            old = {}                                     # accepted by automation now: everything listed so far is new
            self._started(iid)
        if inc["status"] not in ("pending", "active"):
            return
        self._started(iid)                               # else: what was seen stays the baseline (N2, never replayed)
        self._paged(iid)
        times, otimes = new.get("times") or {}, old.get("times") or {}
        uts, outs = new.get("unit_times") or {}, old.get("unit_times") or {}
        had = set(old.get("units") or [])
        fresh = [n for n in units if n not in had]
        unknown = [n for n in fresh if _key(n) not in ours]
        if unknown:
            with self.store.tx() as db:
                self.store._event(db, iid, "auto", f"{label} units not on our boards (not dispatched): {', '.join(unknown)}", op)
        for name in fresh:                               # newly listed: dispatched at the feed's time
            u = ours.get(_key(name))
            if u is not None:
                t = (uts.get(name) or {}).get("dispatched") or times.get("dispatched")
                self._assign(iid, u, t, op, f"dispatched (county {_hm(t)})" if t else "dispatched (county)")
        single = units[0] if len(units) == 1 and _key(units[0]) in ours and not uts.get(units[0]) else None
        for name in dict.fromkeys([*uts, *([single] if single else [])]):
            u = ours.get(_key(name))
            if u is None:
                continue
            if name in uts:
                now_t, was = uts.get(name) or {}, outs.get(name) or {}
            else:
                now_t, was = times, otimes
            for k in ("responding", "on_location", "in_service"):
                t = now_t.get(k)
                if t is not None and t != was.get(k):
                    self._unit_step(iid, u, FEED_STATUS[k], t, op, "county", k)
        if times.get("in_service") is not None or new.get("closed"):
            self._close(iid, times.get("in_service") or self.clock(), op, label, units)

    def _started(self, iid: int) -> None:
        with self.store.tx() as db:
            db.execute("UPDATE auto_incidents SET started_at = ? WHERE incident_id = ? AND started_at IS NULL", (self.clock(), iid))

    def accepted(self, iid: int) -> None:
        """A person accepted it (any mode): automation goes on from what was seen, never replaying it (N2)."""
        with self._lock:
            self._started(iid)

    def _paged(self, iid: int) -> None:
        r = self._tracking(iid)
        if r is None or r["paged_at"] is not None:
            return
        if self.store.one("SELECT 1 AS x FROM auto_steps WHERE incident_id = ? AND kind = 'paged'", (iid,)):
            return                                       # recorded before (a person's undo sticks: L1)
        p = self.store.one("SELECT MIN(first_seen) AS t FROM source_reports WHERE incident_id = ? AND source = ?", (iid, PAGING))
        if not p or p["t"] is None:
            return
        with self.store.tx() as db:
            db.execute("UPDATE auto_incidents SET paged_at = ? WHERE incident_id = ?", (p["t"], iid))
        self._step(iid, "paged", "radio", f"Paged {_hm(p['t'])}", p["t"], own_event=True, operator="Paging · auto")

    # -- one step --------------------------------------------------------------------------------------------------

    def _step(self, iid: int, kind: str, source: str, text: str, at: float, unit: dict | None = None, prev: str = "",
              status: str = "", col: str = "", ref: str = "", event_kind: str = "", own_event: bool = False,
              operator: str = "") -> int:
        with self.store.tx() as db:
            eid = None
            if own_event:
                self.store._event(db, iid, "auto", text, operator, at=at)
                eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            elif event_kind:
                q = "SELECT MAX(id) FROM events WHERE incident_id = ? AND kind = ?" + (" AND unit_id = ?" if unit else "")
                eid = db.execute(q, (iid, event_kind, *([unit["id"]] if unit else []))).fetchone()[0]
            cur = db.execute("INSERT INTO auto_steps (incident_id, at, made_at, kind, source, unit, unit_id, prev, status, col, "
                             "ref, text, event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (iid, at, self.clock(), kind, source, unit["name"] if unit else "", unit["id"] if unit else None,
                              prev, status, col, ref, text, eid))
            return cur.lastrowid

    def _on_here(self, iid: int, uid: int) -> dict | None:
        return self.store.one("SELECT * FROM assignments WHERE incident_id = ? AND unit_id = ? AND cleared_at IS NULL", (iid, uid))

    def _assign(self, iid: int, u: dict, t: float | None, op: str, verb: str) -> bool:
        L = self.store.lists
        u = self.store.unit(u["id"])
        if self._manual(iid, f"unit:{u['name']}") or u["incident_id"] == iid:
            return u["incident_id"] == iid
        if self.store.one("SELECT 1 AS x FROM assignments WHERE incident_id = ? AND unit_id = ?", (iid, u["id"])):
            return False                                 # was on it and cleared: never put back by automation (H1)
        if L.role(u["status"]) == "oos":
            return False
        if u["incident_id"]:
            other = self.store.incident(u["incident_id"])
            if other["status"] in OPEN:
                with self.store.tx() as db:
                    self.store._event(db, iid, "auto", f"{u['name']} on {other['number'] or '#' + str(other['id'])}: not moved", op)
                return False
        self.store.assign(iid, [u["id"]], op, verb=verb)
        a = self._on_here(iid, u["id"])
        if a is not None and t:
            with self.store.tx() as db:
                db.execute("UPDATE assignments SET dispatched_at = ? WHERE id = ?", (t, a["id"]))
        self._step(iid, "assign", "county", f"{u['name']} {verb}", t or self.clock(), unit=u, prev=u["status"],
                   status=L.status_for("dispatched"), col="dispatched_at", event_kind="status")
        return True

    def _code(self, intent: str) -> str:
        return self.cad.hints.status_for(intent)

    def _rank(self, status: str) -> int:
        L = self.store.lists
        if status == L.command_status:
            return 4
        return RANK.get(L.role(status), 0)

    def _unit_step(self, iid: int, u: dict, intent: str, t: float, op: str, source: str, ref: str) -> None:
        """A unit's status from the feed's time: only a unit on this incident now, forward only against this
        assignment's own times and its status (H1: never (re)assigns; a late time never undoes a later step)."""
        L = self.store.lists
        if self._manual(iid, f"unit:{u['name']}"):
            return
        code = self._code(intent)
        if not code:
            return
        u = self.store.unit(u["id"])
        a = self._on_here(iid, u["id"])
        if a is None or u["incident_id"] != iid:
            return                                       # not on it (never was, or cleared): nothing to move
        if intent == "enroute" and (a["enroute_at"] is not None or a["onscene_at"] is not None):
            return
        if intent == "onscene" and a["onscene_at"] is not None:
            return
        if intent != "free" and self._rank(code) <= self._rank(u["status"]):
            return                                       # already there, or further along
        col = COLS.get(L.role(code), "")
        fresh = bool(col) and a.get(col) is None
        if self._manual(iid, f"unit:{u['name']}"):
            return                                       # re-checked just before the write (L4)
        self.store.set_unit_status(u["id"], code, op, why=f"County {_hm(t)}")
        if fresh:
            with self.store.tx() as db:
                db.execute(f"UPDATE assignments SET {col} = ? WHERE id = ?", (t, a["id"]))
        self._step(iid, "status", source, f"{u['name']} {L.status_phrase(code, u['name'], u['board'])}", t, unit=u,
                   prev=u["status"], status=code, col=col if fresh else "", ref=ref, event_kind="status")

    def _close(self, iid: int, t: float, op: str, label: str, listed) -> None:
        """The feed's incident in service (N1): every unit the feed lists that no person marked is released (in service,
        undoable); the call closes when none is left on it. A unit a person put or kept there, or one the feed doesn't
        list, keeps it open with "close?" (shown on the incident and its queue card, one click closes it);
        recheck_close() closes it once those clear."""
        if self._manual(iid, "close"):
            return
        inc = self.store.incident(iid)
        if inc["status"] not in ("pending", "active"):
            return
        if self.store.one("SELECT 1 AS x FROM auto_steps WHERE incident_id = ? AND kind = 'close'", (iid,)):
            return                                       # closed by automation before (and reopened since): a person's
        ours = self._ours()
        keys = {_key(n) for n in listed or []}
        for r in self.store.q("SELECT u.id, u.name FROM assignments a JOIN units u ON u.id = a.unit_id "
                              "WHERE a.incident_id = ? AND a.cleared_at IS NULL ORDER BY a.id", (iid,)):
            on_list = any(ours.get(k, {}).get("id") == r["id"] for k in keys)
            if on_list and not self._manual(iid, f"unit:{r['name']}"):
                self._unit_step(iid, self.store.unit(r["id"]), "free", t, op, "county", "in_service")
        blocking = self._blocking(iid)
        if blocking:
            if not self.store.one("SELECT 1 AS x FROM auto_steps WHERE incident_id = ? AND kind = 'close?' AND ref = ?",
                                  (iid, str(t))):
                self._step(iid, "close?", "county", f"County in service {_hm(t)}: close? (still on it: {', '.join(blocking)})",
                           t, ref=str(t), own_event=True, operator=op)
            return
        self._do_close(iid, t, op, label)

    def _blocking(self, iid: int) -> list[str]:
        return [r["name"] for r in self.store.q(
            "SELECT u.name FROM assignments a JOIN units u ON u.id = a.unit_id WHERE a.incident_id = ? AND a.cleared_at IS NULL "
            "ORDER BY a.id", (iid,))]

    def _do_close(self, iid: int, t: float, op: str, label: str) -> None:
        if self._manual(iid, "close"):
            return
        codes = self.store.lists.disposition_codes
        disp = "COMPLETE" if "COMPLETE" in codes else codes[0]
        with self.store.lock:                            # what the close releases, to put back on an undo
            left = [{"unit_id": r["id"], "status": r["status"]} for r in self.store._left_on(self.store.db, iid)]
        self.store.close(iid, disp, op)
        self._step(iid, "close", "county", f"Closed: {label} in service {_hm(t)}", t, event_kind="closed",
                   ref=json.dumps(left))

    def close_pending(self, iid: int) -> dict | None:
        """The latest "close?" on an open incident not closed by automation since: the step, else None."""
        inc = self.store.one("SELECT status FROM incidents WHERE id = ?", (iid,))
        if not inc or inc["status"] not in ("pending", "active") or self._manual(iid, "close"):
            return None
        if self.store.one("SELECT 1 AS x FROM auto_steps WHERE incident_id = ? AND kind = 'close'", (iid,)):
            return None
        return self.store.one("SELECT * FROM auto_steps WHERE incident_id = ? AND kind = 'close?' ORDER BY id DESC LIMIT 1", (iid,))

    def recheck_close(self, iids) -> None:
        """A unit cleared off these incidents (the radio, the board, by hand): a pending close goes ahead once no unit is
        left on it (60 Control mode, a tracked incident). Never raises."""
        try:
            with self._lock:
                if self.mode() != "60control":
                    return
                for iid in {i for i in iids if i}:
                    s = self.close_pending(iid)
                    if s is None or not self.tracked(iid) or self._blocking(iid):
                        continue
                    _, label = self._feed(iid)
                    self._do_close(iid, s["at"], f"{label or 'County feed'} · auto", label or "County feed")
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: dispatch mode: {type(e).__name__}: {e}")

    # -- the radio's status hints -------------------------------------------------------------------------------------

    def on_hints(self, made: list[dict]) -> None:
        """Suggestions just made from a transcript: in 60 Control mode, taken at once for a tracked incident's unit."""
        if not made:
            return
        try:
            with self._lock:
                if self.mode() != "60control":
                    return
                for h in made:
                    self._hint(h)
        except Exception as e:  # noqa: BLE001
            self.log(f"CAD: dispatch mode: {type(e).__name__}: {e}")

    def _hint(self, h: dict) -> None:
        if h.get("outcome", "pending") != "pending" or h["intent"] not in HINT_INTENTS:
            return
        if h["intent"] == "free" and h["phrase"].startswith("clear"):
            return                                       # "clear" alone is loose: stays a suggestion
        u = self.store.unit(h["unit_id"])
        if u["response"] != "fire":
            return
        on = u["incident_id"]
        if on is not None:                               # on a call: its status, on that call only (H4)
            iid = on
            if not self.tracked(iid):
                return
        else:                                            # on no call: placed only by its own "responding", one call open
            if h["intent"] != "enroute" or h["who"] not in ("talker", "named"):
                return
            t = self.store.one("SELECT rig, radio_id, talker FROM radio_traffic WHERE id = ?", (h["traffic_id"],))
            known = bool(t and (t["rig"] or (t["talker"] and self.store.one(
                "SELECT 1 AS x FROM radios WHERE (alias != '' AND alias = ?) OR (unit != '' AND unit = ?)",
                (t["talker"], t["talker"])))))
            if not known:
                return                                   # a radio nobody named (no rig, an unknown ID or alias): never places (#163)
            since = self.clock() - ASSIGN_WINDOW_S
            cands = [r["id"] for r in self.store.q("SELECT id FROM incidents WHERE status IN ('pending', 'active') "
                                                   "AND created_at >= ?", (since,))]
            if len(cands) != 1 or not self.tracked(cands[0]):
                return                                   # none, or which? (any open call counts, tracked or not)
            iid = cands[0]
        if self.store.incident(iid)["status"] not in ("pending", "active"):
            return
        if on is None and self.store.one("SELECT 1 AS x FROM assignments WHERE incident_id = ? AND unit_id = ?", (iid, u["id"])):
            return                                       # it was on this one and cleared: a person puts it back
        if self._manual(iid, f"unit:{u['name']}"):
            return
        a = self._on_here(iid, u["id"])
        L = self.store.lists
        col = COLS.get(L.role(h["status"]), "")
        fresh = bool(col) and col != "cleared_at" and (a is None or a.get(col) is None)
        with self.store.tx() as db:
            db.execute("UPDATE status_hints SET incident_id = ? WHERE id = ?", (iid, h["id"]))
        if self._manual(iid, f"unit:{u['name']}"):
            return                                       # re-checked just before the write (L4)
        try:
            self.cad.hints.accept(h["id"], "Radio · auto")
        except CadError:
            return
        if h["intent"] in ("free", "quarters"):
            self.recheck_close([iid])                    # the last unit off: a pending close goes ahead (N1)
        a = self._on_here(iid, u["id"]) or a
        if fresh and a is not None:
            with self.store.tx() as db:
                db.execute(f"UPDATE assignments SET {col} = ? WHERE id = ? AND {col} IS NOT NULL", (h["heard_at"], a["id"]))
        self._step(iid, "status", "radio", f"{u['name']} {L.status_phrase(h['status'], u['name'], u['board'])}: “{h['said']}”",
                   h["heard_at"], unit=u, prev=u["status"], status=h["status"], col=col if fresh else "", ref=h["traffic_id"],
                   event_kind="status")

    # -- one click: undo -------------------------------------------------------------------------------------------

    def undo(self, iid: int, sid: int, operator: str = "") -> dict:
        """POST /api/cad/incident/<id>/auto-undo {step}: an automatic step taken back; the unit (or the close) is then
        a person's."""
        with self._lock:
            s = self.store.one("SELECT * FROM auto_steps WHERE id = ? AND incident_id = ?", (sid, iid))
            if s is None:
                raise CadError(f"no automatic step {sid} on this event")
            if s["undone_at"] is not None:
                raise CadError("already undone")
            L = self.store.lists
            inc = self.store.incident(iid)
            if s["kind"] in ("accept", "close?"):
                raise CadError("an accepted event stays accepted: close it" if s["kind"] == "accept" else "nothing to undo")
            if s["unit"]:
                at = self._marked_at(iid, f"unit:{s['unit']}")
                if at is not None and at > s["made_at"]:
                    raise CadError(f"{s['unit']} was changed by hand since: set its status")   # L2
            if s["kind"] == "close":
                if inc["status"] != "closed":
                    raise CadError(f"event is {inc['status']}")
                self.store.reopen(iid, operator)
                self.mark(iid, ["close"], operator)
                self._restore(iid, s, operator)
            elif s["kind"] == "paged":
                with self.store.tx() as db:
                    db.execute("UPDATE auto_incidents SET paged_at = NULL WHERE incident_id = ?", (iid,))
            else:
                u = self.store.unit(s["unit_id"])
                a = self._on_here(iid, u["id"])
                if s["kind"] == "assign":
                    if a is None:
                        raise CadError(f"{u['name']} is no longer on this event")
                    if u["status"] != s["status"]:
                        raise CadError(f"{u['name']} has moved on since: set its status")
                    back = s["prev"] if L.role(s["prev"]) in ("free", "oos") else L.free_for(u["name"], u["board"])
                    now = self.clock()
                    with self.store.tx() as db:              # cleared, not deleted: its record stays (L2)
                        db.execute("UPDATE assignments SET cleared_at = ? WHERE id = ?", (now, a["id"]))
                        db.execute("UPDATE units SET status = ?, status_at = ? WHERE id = ?", (back, now, u["id"]))
                        self.store._history(db, u["id"], u["status"], back, iid, operator, now, "automatic dispatch undone")
                        self.store._event(db, iid, "status", f"{u['name']} off this call (automatic dispatch undone)",
                                          operator, u["id"], now)
                elif L.role(s["status"]) == "free":      # cleared by the feed / the radio: back on it as it was
                    if u["incident_id"] is not None:
                        raise CadError(f"{u['name']} is on another call now")
                    if s["prev"] in self.store.return_statuses():
                        self.store.return_to_incident(iid, u["id"], s["prev"], operator)
                    else:
                        self.store.set_unit_status_on(u["id"], s["prev"], iid, operator)
                else:
                    if a is None:
                        raise CadError(f"{u['name']} is no longer on this event")
                    if u["status"] != s["status"]:
                        raise CadError(f"{u['name']} has moved on since: set its status")
                    self.store.set_unit_status(u["id"], s["prev"], operator, why="Automatic status undone")
                    if s["col"]:
                        with self.store.tx() as db:
                            db.execute(f"UPDATE assignments SET {s['col']} = NULL WHERE id = ?", (a["id"],))
                self.mark_units(iid, [u["name"]], operator)
            now = self.clock()
            with self.store.tx() as db:
                db.execute("UPDATE auto_steps SET undone_at = ?, undone_by = ? WHERE id = ?", (now, operator, sid))
                self.store._event(db, iid, "auto", f"Undone: {s['text']}", operator, at=now)
            return {"ok": True, "step": sid, "incident_id": iid}

    def _restore(self, iid: int, s: dict, operator: str) -> None:
        """An automatic close undone: each unit it released back as it was, when still free and on no other call."""
        try:
            left = json.loads(s["ref"] or "[]")
        except ValueError:
            left = []
        L = self.store.lists
        now = self.clock()
        for r in left if isinstance(left, list) else []:
            u = self.store.unit(int(r["unit_id"]))
            if u["incident_id"] is None and L.role(u["status"]) == "free" and r.get("status") in L.status_ids:
                with self.store.tx() as db:
                    db.execute("UPDATE units SET status = ?, status_at = ? WHERE id = ?", (r["status"], now, u["id"]))
                    self.store._history(db, u["id"], u["status"], r["status"], iid, operator, now, "automatic close undone")

    # -- shown -------------------------------------------------------------------------------------------------------

    def decorate(self, incs: list[dict]) -> list[dict]:
        """inc["auto"] = {tracked, paged_at, steps: [{id, at, kind, source, unit, text, event_id, undone, undo}]}."""
        ids = [i["id"] for i in incs]
        rows: dict[int, dict] = {}
        steps: dict[int, list] = {}
        closed_by_hand: set[int] = set()
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            marks = ",".join("?" * len(part))
            for r in self.store.q(f"SELECT * FROM auto_incidents WHERE incident_id IN ({marks})", part):
                rows[r["incident_id"]] = r
            closed_by_hand |= {r["incident_id"] for r in self.store.q(
                f"SELECT incident_id FROM manual_edits WHERE field = 'close' AND incident_id IN ({marks})", part)}
            for s in self.store.q(f"SELECT * FROM auto_steps WHERE incident_id IN ({marks}) ORDER BY id", part):
                steps.setdefault(s["incident_id"], []).append(
                    {"id": s["id"], "at": s["at"], "kind": s["kind"], "source": s["source"], "unit": s["unit"],
                     "status": s["status"], "text": s["text"], "event_id": s["event_id"], "undone": s["undone_at"] is not None,
                     "undo": s["kind"] not in ("accept", "close?") and s["undone_at"] is None,
                     "ask_close": s["kind"] == "close?"})
        for inc in incs:
            r = rows.get(inc["id"])
            st = steps.get(inc["id"], [])
            ask = next((x for x in reversed(st) if x["kind"] == "close?"), None)
            pending = (ask is not None and inc["status"] in ("pending", "active") and not any(x["kind"] == "close" for x in st)
                       and inc["id"] not in closed_by_hand)
            inc["auto"] = None if r is None and inc["id"] not in steps else {
                "tracked": bool(r and r["tracked_at"] is not None), "paged_at": r["paged_at"] if r else None,
                "steps": st, "close_pending": ask if pending else None}
        return incs

    @staticmethod
    def is_feed(inc: dict) -> bool:
        return inc.get("source") == FEED_SOURCE
