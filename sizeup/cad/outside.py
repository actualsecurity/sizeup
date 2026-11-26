"""Outside units: another department's units on one of our calls (mutual aid), assigned to the incident beside ours but
never one of ours. On the CAD of record, in both dispatch modes.

    feed       each unit code an external CAD feed lists on an accepted call of ours (its unit list, and its
               "group_units" extra: every department's units on the call), every live report of the call (a two-town
               call's runs too, sources.py "cross town"), that is not ours: assigned as an outside unit, with the feed's
               own times for it (unit_times: dispatched, responding, on location, in service). Not ours: not one of the
               units automode dispatches (AutoDispatch._ours), never a department entry or a department's placeholder
               (DPT...: sources.is_placeholder), never a mutual-aid request code (kind "ma-request"), another agency's
               board unit (EMS, PD) or a code of one of our own departments; those stay one "not assigned" line
               (automode). Unlisted later (and assigned by the feed): cleared then; listed again: back on
    radio      a transmission from a mutual-aid board unit's radio (the registry's rig: a unit on a mutual-aid board,
               response "mutual_aid") on an accepted call's channels or about it (radio_links: its fireground, its
               operating channel, its address or place or units named, the airport talkgroup, a person's link):
               assigned "from radio" when no feed lists it. Its own words, when they clearly say it of itself
               (statushints.Phrases: "<unit> on location", "on location" on its own radio; never another's, never
               60 Control's), set its responding / on location / command times, forward only
    who        an outside unit is a row of outside_units, keyed by the feed's code and its department: a mutual-aid
               board unit's department, else the county's apparatus and department numbers
               (data/westchester_apparatus.csv: "Engine 901"; a chief's number: the department number and a digit),
               else a "group_units" group that names another department (Sources.department_units), else none. An
               admin may give it a name and a department to show (POST /api/cad/outside-units); it is never a
               board unit: never on our boards or roster, never our staffing (store._staffed), never located,
               followed, reminded (#865), paged or pushed
    shown      inc["outside"]: after our units in Assigned, "MA · <department>" (a mutual-aid board unit a person put on
               the call is shown once, as ours). A closed call's are shown cleared at its close
"""

from __future__ import annotations

import csv
import json
import re
import threading
from pathlib import Path

from sizeup import sourcekeys
from sizeup.cad.sources import PAGING, dept_code, is_placeholder

ROOT = Path(__file__).resolve().parent.parent.parent
APPARATUS = ROOT / "data" / "westchester_apparatus.csv"
DEPARTMENTS = ROOT / "data" / "westchester_departments.csv"
FEED_TIMES = {"dispatched": "dispatched_at", "responding": "enroute_at", "on_location": "onscene_at",
              "in_service": "cleared_at"}
RADIO_WHYS = ("fireground", "opchannel", "mention", "airport", "manual")   # a transmission on the call or about it
REQUEST_KIND = "ma-request"
SAID = {"enroute": ("enroute_at", "responding"), "onscene": ("onscene_at", "on location"),
        "command": ("command_at", "command")}
NAME_MAX = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS outside_units (      -- another department's unit seen on our calls: never a board unit
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL,                   -- as the feed (or the mutual-aid board) writes it
  department TEXT NOT NULL DEFAULT '',  -- whose, as worked out ('' = not known): with code, the key
  unit TEXT NOT NULL DEFAULT '',        -- the mutual-aid board unit it is ('' = none)
  name TEXT NOT NULL DEFAULT '',        -- an admin's name for it ('' = the code)
  shown_dept TEXT NOT NULL DEFAULT '',  -- an admin's department for it ('' = department)
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  UNIQUE (code, department)
);
CREATE TABLE IF NOT EXISTS outside_assignments (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL,
  outside_id INTEGER NOT NULL,
  via TEXT NOT NULL,                    -- feed | radio (heard, no feed lists it)
  listed INTEGER NOT NULL DEFAULT 0,    -- a feed lists it now
  unlisted INTEGER NOT NULL DEFAULT 0,  -- cleared because no feed lists it any more (listed again: back on)
  assigned_at REAL NOT NULL,
  dispatched_at REAL, enroute_at REAL, onscene_at REAL, command_at REAL, cleared_at REAL,
  traffic_id TEXT NOT NULL DEFAULT '',  -- radio: the transmission it was first heard on
  UNIQUE (incident_id, outside_id)
);
CREATE INDEX IF NOT EXISTS outside_assignments_incident ON outside_assignments(incident_id);
"""


def _key(s) -> str:
    return re.sub(r"[\s-]+", "", str(s or "")).upper()


def _abbrev(name: str) -> str:
    """'Engine 901' -> 'E901', 'Tower Ladder 9' -> 'TL9' (the county's apparatus as its CAD writes it)."""
    m = re.fullmatch(r"\s*([A-Za-z][A-Za-z ]*?)\s+(\d+)\s*", name or "")
    return "".join(w[0] for w in m[1].split()).upper() + m[2] if m else ""


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return list(csv.DictReader(ln for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")))


def county_departments(apparatus: Path = APPARATUS, departments: Path = DEPARTMENTS) -> tuple[dict, dict]:
    """({unit code as written: department}, {department number: department}) from the county's mutual aid plan: a
    code two departments would share is left out."""
    units: dict[str, str] = {}
    clash: set[str] = set()
    for r in _rows(apparatus):
        dep, unit = (r.get("department") or "").strip(), (r.get("unit") or "").strip()
        for k in {_key(unit), _key(_abbrev(unit))} - {""}:
            if units.get(k, dep) != dep:
                clash.add(k)
            units[k] = dep
    numbers = {(r.get("dept_number") or "").strip(): (r.get("department") or "").strip()
               for r in _rows(departments) if (r.get("dept_number") or "").strip()}
    return {k: v for k, v in units.items() if k not in clash and v}, numbers


def dept_key(name: str) -> str:
    from sizeup.cad.scope import dept_key as key
    return key(name)


class OutsideUnits:
    def __init__(self, cad, clock=None):
        self.cad, self.store = cad, cad.store
        self.clock = clock or cad.clock
        self._lock = threading.RLock()
        self._county: tuple[dict, dict] | None = None
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    # -- whose ------------------------------------------------------------------------------------------------------

    def county(self) -> tuple[dict, dict]:
        if self._county is None:
            try:
                self._county = county_departments()
            except (OSError, csv.Error, UnicodeDecodeError):
                self._county = ({}, {})
        return self._county

    def _ours(self) -> set[str]:
        """Our departments as compared (scope.dept_key): the home department, the monitored ones, the joint towns."""
        from sizeup.cad.lists import HOME_DEPARTMENT
        names = {HOME_DEPARTMENT, *self.cad.s.monitor_departments, *getattr(self.cad, "monitor_names", ()),
                 *self.store.lists.joint_response}
        return {dept_key(n) for n in names} - {""}

    def county_dept(self, code: str) -> str:
        """The county's department for a code: an apparatus number ("E901", "Engine 901"), or a chief's number (the
        department number and a digit or two); '' when neither says."""
        units, numbers = self.county()
        k = _key(code)
        if k in units:
            return units[k]
        m = re.fullmatch(r"(\d{3})\d{1,2}", k)
        return numbers.get(m[1], "") if m else ""

    def _board(self) -> dict[str, dict]:
        """A code as written (spaces, dashes and case aside) -> the board unit (its code, its spoken name, an alias)."""
        units = [u for u in self.store.units() if u["board"]]
        out: dict[str, dict] = {}
        for u in units:
            for k in (_key(u["name"]), _key(u["spoken"])):
                if k:
                    out.setdefault(k, u)
        by = {u["name"]: u for u in units}
        try:
            for k, code in self.cad.sources.feed_unit_aliases().items():
                if code in by:
                    out[_key(k)] = by[code]
        except Exception:  # noqa: BLE001 - no aliases: the codes alone
            pass
        return out

    def classify(self, code: str, group: str = "", ctx: dict | None = None) -> tuple[str, str, str]:
        """(what, department, board unit) for a code a feed lists on our call: what is "ours" (automode's to
        dispatch), "skip" (a department entry or placeholder: never a unit), "other" (not assigned: another agency's
        board unit, a mutual-aid request code, an unknown code of our own department) or "outside"."""
        ctx = ctx if ctx is not None else self.context()
        k = _key(code)
        if not k or k in ctx["depts"] or is_placeholder(code):
            return "skip", "", ""
        if k in ctx["ours"]:
            return "ours", "", ""
        u = ctx["board"].get(k)
        if u is not None:
            if self.store.lists.placeholder(u["name"]):
                return "skip", "", ""
            if u["response"] != "mutual_aid" or u["kind"] == REQUEST_KIND:
                return "other", "", u["name"]
            return "outside", self.store.lists.department_of(u["name"]) or self.county_dept(u["name"]), u["name"]
        dept = self.county_dept(code)
        if not dept and group:
            g = ctx["depts_named"].get(dept_code(group), "")
            dept = g if dept_key(g) not in ctx["our_depts"] else ""
        if dept and dept_key(dept) in ctx["our_depts"]:
            return "other", dept, ""
        return "outside", dept, ""

    def context(self) -> dict:
        auto = self.cad.auto
        try:
            named = {dept_code(k): v for k, v in self.cad.sources.department_units().items()}
        except Exception:  # noqa: BLE001
            named = {}
        return {"ours": auto._ours(), "depts": set(auto._depts()), "board": self._board(), "depts_named": named,
                "our_depts": self._ours()}

    def is_outside(self, code: str, ctx: dict | None = None) -> bool:
        return self.classify(code, ctx=ctx)[0] == "outside"

    # -- the feed ---------------------------------------------------------------------------------------------------

    def _open(self, iid: int) -> bool:
        """An accepted call of ours that takes units (never a notification: automode never puts units on one)."""
        inc = self.store.incident(iid)
        if inc["status"] not in ("pending", "active"):
            return False
        return inc["emergency"] or not self.store.lists.is_notification(inc["type"])

    def listed(self, iid: int, ctx: dict | None = None) -> tuple[dict[str, dict], str]:
        """The outside units the incident's live feed reports list now: {code: {department, unit, times}}, and the
        best-ranked report's label."""
        ctx = ctx if ctx is not None else self.context()
        out: dict[str, dict] = {}
        label = ""
        for r in self.store.q("SELECT data, label FROM source_reports WHERE incident_id = ? AND source != ? "
                              "AND merged_into = '' ORDER BY rank, first_seen", (iid, PAGING)):
            d = json.loads(r["data"] or "{}")
            label = label or r["label"] or "County feed"
            uts = {_key(c): t for c, t in (d.get("unit_times") or {}).items() if isinstance(t, dict)}
            codes = [(c, "") for c in d.get("units") or []]
            groups = (d.get("extra") or {}).get("group_units")
            if isinstance(groups, dict):
                codes += [(c, g) for g, cs in groups.items() if isinstance(cs, list) for c in cs if isinstance(c, str)]
            for code, group in codes:
                what, dept, unit = self.classify(code, group, ctx)
                if what != "outside":
                    continue
                ts = uts.get(_key(code)) or uts.get(_key(unit)) or {}
                code = unit or str(code).strip()
                have = out.setdefault(code, {"department": dept, "unit": unit, "times": {}})
                for kk, col in FEED_TIMES.items():
                    t = ts.get(kk)
                    if isinstance(t, (int, float)) and t > 0 and col not in have["times"]:
                        have["times"][col] = float(t)
        return out, label

    def sync(self, iid: int | None) -> list[str]:
        """The incident's outside units brought in line with what its feeds list now (Cad.source_event, an accept).
        The codes newly assigned. Never raises."""
        if not iid:
            return []
        try:
            with self._lock:
                return self._sync(iid)
        except Exception as e:  # noqa: BLE001 - the report stands whatever this does
            self.cad.log(f"CAD: outside units: {type(e).__name__}")
            return []

    def _sync(self, iid: int) -> list[str]:
        if not self._open(iid):
            return []
        listed, label = self.listed(iid)
        if not listed and not self.store.one("SELECT 1 AS x FROM outside_assignments WHERE incident_id = ? AND listed = 1",
                                             (iid,)):
            return []
        now = self.clock()
        board_on = {r["name"] for r in self.store.q(
            "SELECT u.name FROM assignments a JOIN units u ON u.id = a.unit_id WHERE a.incident_id = ?", (iid,))}
        new: list[tuple[str, str]] = []
        with self.store.tx() as db:
            seen: set[int] = set()
            for code, x in listed.items():
                if x["unit"] and x["unit"] in board_on:
                    continue                             # a person put that mutual-aid board unit on: shown as ours
                oid = self._unit_id(db, code, x["department"], x["unit"], now)
                seen.add(oid)
                t = x["times"]
                a = db.execute("SELECT * FROM outside_assignments WHERE incident_id = ? AND outside_id = ?",
                               (iid, oid)).fetchone()
                if a is None:
                    db.execute("INSERT INTO outside_assignments (incident_id, outside_id, via, listed, assigned_at, "
                               "dispatched_at, enroute_at, onscene_at, cleared_at) VALUES (?, ?, 'feed', 1, ?, ?, ?, ?, ?)",
                               (iid, oid, now, t.get("dispatched_at"), t.get("enroute_at"), t.get("onscene_at"),
                                t.get("cleared_at")))
                    new.append((code, x["department"]))
                    continue
                cols = {"via": "feed", "listed": 1}
                for col in ("dispatched_at", "enroute_at", "onscene_at"):
                    if col in t and a[col] != t[col]:
                        cols[col] = t[col]
                if "cleared_at" in t:
                    cols.update(cleared_at=t["cleared_at"], unlisted=0)
                elif a["unlisted"]:
                    cols.update(cleared_at=None, unlisted=0)      # listed again: back on
                db.execute(f"UPDATE outside_assignments SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                           (*cols.values(), a["id"]))
            for a in db.execute("SELECT id, outside_id, via, cleared_at FROM outside_assignments WHERE incident_id = ? "
                                "AND listed = 1", (iid,)).fetchall():
                if a["outside_id"] in seen:
                    continue
                if a["via"] == "feed" and a["cleared_at"] is None:
                    db.execute("UPDATE outside_assignments SET listed = 0, unlisted = 1, cleared_at = ? WHERE id = ?",
                               (now, a["id"]))
                else:
                    db.execute("UPDATE outside_assignments SET listed = 0 WHERE id = ?", (a["id"],))
            if new:
                self.store._event(db, iid, "auto", f"Mutual aid ({label}): {_grouped(new)}", f"{label} · auto")
        return [c for c, _ in new]

    def _unit_id(self, db, code: str, dept: str, unit: str, now: float) -> int:
        r = db.execute("SELECT id FROM outside_units WHERE code = ? AND department = ?", (code, dept)).fetchone()
        if r is not None:
            db.execute("UPDATE outside_units SET last_seen = ?, unit = CASE WHEN ? != '' THEN ? ELSE unit END WHERE id = ?",
                       (now, unit, unit, r[0]))
            return r[0]
        return db.execute("INSERT INTO outside_units (code, department, unit, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
                          (code, dept, unit, now, now)).lastrowid

    # -- the radio --------------------------------------------------------------------------------------------------

    def heard(self, tid: str) -> list[int]:
        """A transmission on our calls (its links made): a mutual-aid board unit's radio on an accepted call's
        channels or about it is assigned there "from radio" (unless a feed lists it: then it already is), and its own
        clear words about itself move its times. The incidents it was newly assigned to. Never raises."""
        try:
            with self._lock:
                return self._heard(tid)
        except Exception as e:  # noqa: BLE001 - the transmission stands whatever this does
            self.cad.log(f"CAD: outside units: {type(e).__name__}")
            return []

    def _heard(self, tid: str) -> list[int]:
        t = self.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))
        if t is None or t["dispatcher"] or not (t["rig"] or "").strip() or sourcekeys.is_context(t):
            return []
        rigs = [r.strip() for r in t["rig"].split(",") if r.strip()]
        by = {u["name"]: u for u in self.store.units()}
        L = self.store.lists
        mine = [by[r] for r in rigs if r in by and by[r]["board"] and by[r]["response"] == "mutual_aid"
                and by[r]["kind"] != REQUEST_KIND and not L.placeholder(r)]
        if not mine:
            return []
        links = self.store.q(f"SELECT incident_id, why FROM radio_links WHERE traffic_id = ? AND why IN "
                             f"({','.join('?' * len(RADIO_WHYS))})", (tid, *RADIO_WHYS))
        got = []
        for iid in dict.fromkeys(r["incident_id"] for r in links):
            if not self._open(iid):
                continue
            for u in mine:
                if self._radio_one(iid, u, t, talker=len(rigs) == 1):
                    got.append(iid)
        return list(dict.fromkeys(got))

    def _radio_one(self, iid: int, u: dict, t: dict, talker: bool) -> bool:
        code, now = u["name"], self.clock()
        if self.store.one("SELECT 1 AS x FROM assignments WHERE incident_id = ? AND unit_id = ?", (iid, u["id"])):
            return False                                 # on it as a board unit (a person's): shown as ours
        dept = self.store.lists.department_of(code) or self.county_dept(code)
        new = False
        with self.store.tx() as db:
            a = db.execute("SELECT a.* FROM outside_assignments a JOIN outside_units o ON o.id = a.outside_id "
                           "WHERE a.incident_id = ? AND (o.unit = ? OR o.code = ?) ORDER BY a.id LIMIT 1",
                           (iid, code, code)).fetchone()
            if a is None:
                busy = db.execute("SELECT 1 FROM outside_assignments a JOIN outside_units o ON o.id = a.outside_id "
                                  "JOIN incidents i ON i.id = a.incident_id WHERE a.incident_id != ? AND "
                                  "(o.unit = ? OR o.code = ?) AND a.cleared_at IS NULL AND i.status IN ('pending', 'active')",
                                  (iid, code, code)).fetchone()
                if busy:
                    return False                         # on another open call: not this one's by its radio alone
                oid = self._unit_id(db, code, dept, code, now)
                db.execute("INSERT INTO outside_assignments (incident_id, outside_id, via, listed, assigned_at, traffic_id) "
                           "VALUES (?, ?, 'radio', 0, ?, ?)", (iid, oid, t["start"], t["id"]))
                self.store._event(db, iid, "auto", f"Mutual aid from radio: {_grouped([(code, dept)])}", "Radio · auto",
                                  at=t["start"])
                a = db.execute("SELECT * FROM outside_assignments WHERE incident_id = ? AND outside_id = ?",
                               (iid, oid)).fetchone()
                new = True
            if talker and a["cleared_at"] is None:
                for intent in self._said(u, t):
                    col, words = SAID[intent]
                    if a[col] is None:
                        db.execute(f"UPDATE outside_assignments SET {col} = ? WHERE id = ?", (t["start"], a["id"]))
                        self.store._event(db, iid, "auto", f"{code} {words} (radio)", "Radio · auto", at=t["start"])
                    if intent == "command" and a["onscene_at"] is None:
                        db.execute("UPDATE outside_assignments SET onscene_at = ? WHERE id = ?", (t["start"], a["id"]))
        return new

    def _said(self, u: dict, t: dict) -> list[str]:
        """What the unit's own radio clearly says of it (statushints.Phrases, this unit alone): "Engine 901 on location",
        "on location", "we're on scene", "Engine 901 has command"; never of another, never "in the area"."""
        from sizeup.cad.statushints import Phrases
        from sizeup.stt_correct import shown_text
        text = shown_text(t)
        if not text.strip():
            return []
        p = Phrases({u["name"]: u["spoken"] or u["name"]}, kinds={u["name"]: u["kind"]})
        out = []
        for f in p.find(text):
            if f.intent not in SAID or f.near:
                continue
            if f.units == [u["name"]] or (not f.units and f.lead in ("start", "first", "control") and not f.addressee):
                out.append(f.intent)
        return list(dict.fromkeys(out))

    # -- an admin's name for one ------------------------------------------------------------------------------------

    def edit(self, body: dict) -> dict:
        """POST /api/cad/outside-units {id, name?, department?, operator?}: the name and department shown for an outside
        unit ('' = as worked out). Audited (Settings > Audit, section outside_units)."""
        from sizeup.cad.store import CadError
        oid = body.get("id")
        if isinstance(oid, bool) or not isinstance(oid, int):
            raise CadError("id: a number")
        r = self.store.one("SELECT * FROM outside_units WHERE id = ?", (oid,))
        if r is None:
            raise CadError("no such outside unit")
        cols = {}
        for k, col in (("name", "name"), ("department", "shown_dept")):
            if k in body:
                v = body[k]
                if not isinstance(v, str) or len(v.strip()) > NAME_MAX:
                    raise CadError(f"{k}: text, at most {NAME_MAX} characters")
                cols[col] = " ".join(v.split())
        op = str(body.get("operator") or "")[:320]
        now = self.clock()
        with self.store.tx() as db:
            for col, v in cols.items():
                if r[col] != v:
                    db.execute(f"UPDATE outside_units SET {col} = ? WHERE id = ?", (v, oid))
                    db.execute("INSERT INTO settings_audit (at, operator, section, key, old, new) VALUES (?, ?, ?, ?, ?, ?)",
                               (now, op, "outside_units", f"{r['code']}/{col}", json.dumps(r[col]), json.dumps(v)))
        out = self.store.one("SELECT * FROM outside_units WHERE id = ?", (oid,))
        return {"ok": True, "id": oid, "code": out["code"], "name": out["name"] or out["code"],
                "department": out["shown_dept"] or out["department"]}

    # -- shown ------------------------------------------------------------------------------------------------------

    def decorate(self, incs: list[dict]) -> list[dict]:
        """inc["outside"] = [{id, unit_id, code, name, department, via, listed, dispatched_at, enroute_at, onscene_at,
        command_at, cleared_at}], in the order they came; a closed call's cleared at its close (shown)."""
        ids = [i["id"] for i in incs]
        rows: dict[int, list] = {}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            for r in self.store.q(f"SELECT a.*, o.code, o.department, o.unit, o.name, o.shown_dept FROM outside_assignments a "
                                  f"JOIN outside_units o ON o.id = a.outside_id WHERE a.incident_id IN "
                                  f"({','.join('?' * len(part))}) ORDER BY a.id", part):
                rows.setdefault(r["incident_id"], []).append(r)
        for inc in incs:
            ours = {a.get("name") for a in inc.get("assignments") or []}
            closed = inc.get("status") in ("closed", "dismissed")
            inc["outside"] = [{
                "id": r["id"], "unit_id": r["outside_id"], "code": r["code"], "name": r["name"] or r["code"],
                "department": r["shown_dept"] or r["department"], "via": r["via"], "listed": bool(r["listed"]),
                "dispatched_at": r["dispatched_at"], "enroute_at": r["enroute_at"], "onscene_at": r["onscene_at"],
                "command_at": r["command_at"],
                "cleared_at": r["cleared_at"] if r["cleared_at"] is not None or not closed
                else (inc.get("closed_at") or r["assigned_at"]),
                "assigned_at": r["assigned_at"]}
                for r in rows.get(inc["id"], []) if not (r["unit"] and r["unit"] in ours) and r["code"] not in ours]
        return incs


def _grouped(units) -> str:
    """'Sample FD E901, L77; E5' from [(code, department)]: by department, in the order they came."""
    by: dict[str, list[str]] = {}
    for code, dept in units:
        by.setdefault(dept, []).append(code)
    return "; ".join(f"{d} {', '.join(cs)}" if d else ", ".join(cs) for d, cs in by.items())
