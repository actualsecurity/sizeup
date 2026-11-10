"""Who was dispatched (#370): a whole department, or specific units. One label per incident, shown on the incident and
its queue card (inc["scope"]).

    page   the page is usually heard first and sets it at once: the departments' tones only, plus a call type ->
           "Full department (<dept>)" for each department whose tones were heard; apparatus named in it ->
           "Specific units"; neither (no type, no apparatus) -> nothing
    feed   an external CAD feed's unit list (its best-ranked report): a department entry (Sources.department_units:
           a code that means a whole department) -> "Full department (<dept>)" for each one listed, apparatus with
           none -> "Specific units". The feed is the record of truth: it confirms or overrides the page's label, and
           a page heard after it never changes it. A disagreement is a line on the timeline (kind "feed")
"""

from __future__ import annotations

import json

from sizeup.cad.sources import PAGING, dept_code
from sizeup.radio.tones import base_name

FULL, UNITS = "full", "units"

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_scope (
  incident_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                   -- full | units
  departments TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL,                 -- page | feed: who set it (the feed's stands)
  at REAL NOT NULL,
  page TEXT NOT NULL DEFAULT ''         -- the page's own reading [kind, departments] (kept when the feed overrides it)
);
"""


def label(kind: str, departments) -> str:
    if kind == FULL:
        return f"Full department ({', '.join(departments)})" if departments else "Full department"
    return "Specific units" if kind == UNITS else ""


def from_page(tone_departments, units, incident_type: str) -> tuple[str, list[str]] | None:
    """(kind, departments) as the page says it, else None."""
    depts = list(dict.fromkeys(base_name(d) for d in tone_departments or () if d))
    if units:
        return UNITS, []
    if depts and incident_type and incident_type != "Unknown":
        return FULL, depts
    return None


def from_units(units, departments: dict[str, str]) -> tuple[str, list[str]] | None:
    """(kind, departments) from a feed's unit list and its department entries {code: department}, else None."""
    codes = {dept_code(k): v for k, v in departments.items() if dept_code(k)}
    depts = list(dict.fromkeys(codes[dept_code(u)] for u in units or () if dept_code(u) in codes))
    if depts:
        return FULL, depts
    if any(dept_code(u) for u in units or ()):
        return UNITS, []
    return None


class Scope:
    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    def _row(self, iid: int) -> dict | None:
        return self.store.one("SELECT * FROM dispatch_scope WHERE incident_id = ?", (iid,))

    def on_page(self, iid: int | None, inc) -> None:
        """A page (the monitor's incident: its tones, the apparatus it named, its type) made or reached this incident."""
        if not iid:
            return
        got = from_page(getattr(inc, "tone_departments", ()), getattr(inc, "units", ()), getattr(inc, "incident_type", ""))
        if got is None:
            return
        kind, depts = got
        reading = json.dumps([kind, depts])
        r = self._row(iid)
        now = self.store.clock()
        with self.store.tx() as db:
            if r is None:
                db.execute("INSERT INTO dispatch_scope (incident_id, kind, departments, source, at, page) VALUES (?, ?, ?, 'page', ?, ?)",
                           (iid, kind, json.dumps(depts), now, reading))
            elif r["source"] == "page":
                if r["page"] != reading:
                    db.execute("UPDATE dispatch_scope SET kind = ?, departments = ?, at = ?, page = ? WHERE incident_id = ?",
                               (kind, json.dumps(depts), now, reading, iid))
            elif r["page"] != reading:                      # the feed's stands; a page that says otherwise is noted
                db.execute("UPDATE dispatch_scope SET page = ? WHERE incident_id = ?", (reading, iid))
                if (kind, depts) != (r["kind"], json.loads(r["departments"])):
                    self.store._event(db, iid, "feed", f"Page: {label(kind, depts)} (feed: "
                                      f"{label(r['kind'], json.loads(r['departments']))})", at=now)

    def on_feed(self, iid: int | None) -> None:
        """A feed's report applied to this incident: its unit list confirms or overrides the label."""
        if not iid:
            return
        rep = self.store.one("SELECT data, label FROM source_reports WHERE incident_id = ? AND source != ? AND merged_into = '' "
                             "ORDER BY rank, first_seen LIMIT 1", (iid, PAGING))
        if rep is None:
            return
        got = from_units(json.loads(rep["data"] or "{}").get("units") or [], self.cad.sources.department_units())
        if got is None:
            return
        kind, depts = got
        r = self._row(iid)
        if r is not None and r["source"] == "feed" and (r["kind"], json.loads(r["departments"])) == (kind, depts):
            return
        now = self.store.clock()
        name = rep["label"] or "County feed"
        with self.store.tx() as db:
            db.execute("INSERT INTO dispatch_scope (incident_id, kind, departments, source, at) VALUES (?, ?, ?, 'feed', ?) "
                       "ON CONFLICT(incident_id) DO UPDATE SET kind = excluded.kind, departments = excluded.departments, "
                       "source = 'feed', at = excluded.at", (iid, kind, json.dumps(depts), now))
            if r is not None and r["source"] == "page" and (kind, depts) != (r["kind"], json.loads(r["departments"])):
                self.store._event(db, iid, "feed", f"{name}: {label(kind, depts)} (page: "
                                  f"{label(r['kind'], json.loads(r['departments']))})", at=now)

    def decorate(self, incs: list[dict]) -> list[dict]:
        """inc["scope"] = {kind, departments, label, source} or None."""
        ids = [i["id"] for i in incs]
        rows: dict[int, dict] = {}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            for r in self.store.q(f"SELECT * FROM dispatch_scope WHERE incident_id IN ({','.join('?' * len(part))})", part):
                rows[r["incident_id"]] = r
        for inc in incs:
            r = rows.get(inc["id"])
            depts = json.loads(r["departments"]) if r else []
            inc["scope"] = {"kind": r["kind"], "departments": depts, "label": label(r["kind"], depts),
                            "source": r["source"]} if r else None
        return incs
