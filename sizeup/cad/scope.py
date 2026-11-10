"""Who was dispatched (#370): a whole department, or specific units. One label per incident, shown on the incident and
its queue card (inc["scope"]).

    page   the page is usually heard first and sets it at once: the departments' tones only, plus a call type ->
           "Full department (<dept>)" for each department whose tones were heard; apparatus named in it ->
           "Specific units"; neither (no type, no apparatus) -> nothing
    feed   an external CAD feed's unit list (its best-ranked report): a department entry (Sources.department_units:
           a code that means a whole department) -> "Full department (<dept>)" for each one listed, apparatus with
           none -> "Specific units". The feed is the record of truth: it confirms or overrides the page's label, and
           a page heard after it never changes it. A disagreement is a line on the timeline (kind "feed"):
           "Feed: units · page: all PCFD"

Shown short ("Full dept", "Specific units"; short()), "Paged: full department (<dept>)" in its hover (label()); the
timeline says it "all PCFD" / "units" (_said()). A page on a feed's incident (one it reached, or its card moved onto)
says who it paged in one timeline line of kind "tones" (page_line), once.
"""

from __future__ import annotations

import json
import re

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
  page TEXT NOT NULL DEFAULT ''         -- the page's own reading [kind, departments, its line] (kept when the feed
                                        -- overrides it; a moved card's goes with it)
);
"""


def label(kind: str, departments) -> str:
    if kind == FULL:
        return f"Full department ({', '.join(departments)})" if departments else "Full department"
    return "Specific units" if kind == UNITS else ""


def abbrev(name: str) -> str:
    """'Port Chester FD' -> 'PCFD'; a tone book entity 'Port Chester · Fire' -> 'PCFD'; 'Rye Brook' -> 'RB'."""
    place, _, service = str(name or "").partition(" · ")
    words = re.findall(r"[A-Za-z0-9]+", place)
    out = "".join(w if w.isupper() and len(w) > 1 else w[0].upper() for w in words)
    if service:
        out += "FD" if service.split(" · ")[0] == "Fire" else "".join(w[0].upper() for w in re.findall(r"[A-Za-z]+", service))
    return out or str(name or "")


def short(kind: str, departments=()) -> str:
    """The chip: 'Full dept' / 'Specific units'."""
    return {FULL: "Full dept", UNITS: "Specific units"}.get(kind, "")


def _said(kind: str, departments) -> str:
    """As the timeline says it: 'units', 'all PCFD', 'all PCFD, RBFD'."""
    if kind == FULL:
        return "all " + ", ".join(abbrev(d) for d in departments) if departments else "all"
    return "units" if kind == UNITS else ""


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


def page_line(departments, units, tone_departments, incident_type: str, feed: str = "") -> str:
    """One timeline line for who a page paged (#370, with #365's page on a feed's incident): "Page: all PCFD",
    "Page: PCFD · units Engine 60, Ladder 31", "Page: tones PCFD" (no call type), and " · feed: units" when the
    feed's label says otherwise. "" when it named nobody."""
    got = from_page(tone_departments, units, incident_type)
    names = ", ".join(abbrev(base_name(d)) for d in dict.fromkeys(departments or ()) if d)
    if got is not None and got[0] == FULL:
        said = _said(*got)
    elif units:
        said = (f"{names} · " if names else "") + f"units {', '.join(units)}"
    elif names:
        said = f"tones {names}"
    else:
        return ""
    return f"Page: {said}" + (f" · feed: {feed}" if feed else "")


class Scope:
    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    def on_page(self, iid: int | None, inc, note: bool = True) -> tuple[str, bool]:
        """A page (the monitor's incident: its tones, the apparatus it named, its type) made or reached this incident.
        (the feed's label as the timeline says it ("units") when it says otherwise than the page, else ""; whether this
        reading is new here). note: that disagreement is its own timeline line (once per reading; a page on a feed's
        incident says it in its one line instead: page_line)."""
        if not iid:
            return "", False
        tones, units, typ = getattr(inc, "tone_departments", ()), getattr(inc, "units", ()), getattr(inc, "incident_type", "")
        got = from_page(tones, units, typ)
        if got is None:
            return "", True                              # nothing to keep: the line says what it heard
        heard = list(dict.fromkeys([*(tones or []), *(getattr(inc, "departments", None) or [])]))
        return self._reading(iid, *got, note=note, line=page_line(heard, list(units or []), tones, typ))

    def _reading(self, iid: int, kind: str, depts: list[str], note: bool = True, line: str = "") -> tuple[str, bool]:
        reading = json.dumps([kind, depts, line])
        now = self.store.clock()
        with self.store.tx() as db:
            r = db.execute("SELECT * FROM dispatch_scope WHERE incident_id = ?", (iid,)).fetchone()
            differs = (r is not None and r["source"] == "feed"
                       and (kind, depts) != (r["kind"], json.loads(r["departments"])))
            feed = _said(r["kind"], json.loads(r["departments"])) if differs else ""
            if r is not None and r["page"] == reading:
                return feed, False
            # the page's reading, and its label unless the feed's stands (one upsert: never two rows, never a race)
            db.execute("INSERT INTO dispatch_scope (incident_id, kind, departments, source, at, page) VALUES (?, ?, ?, 'page', ?, ?) "
                       "ON CONFLICT(incident_id) DO UPDATE SET page = excluded.page, "
                       "kind = CASE WHEN source = 'feed' THEN kind ELSE excluded.kind END, "
                       "departments = CASE WHEN source = 'feed' THEN departments ELSE excluded.departments END, "
                       "at = CASE WHEN source = 'feed' THEN at ELSE excluded.at END",
                       (iid, kind, json.dumps(depts), now, reading))
            if differs and note:
                self.store._event(db, iid, "feed", f"Page: {_said(kind, depts)} · feed: {feed}", at=now)
        return feed, True

    def move(self, src: int, dst: int) -> None:
        """A page's card moved onto another incident (Sources.rematch_page, _sweep_pages): its page's reading goes
        with it (the feed's label there stands) and its one line, kind "tones", with " · feed: ..." when the feed
        says otherwise (once: never if that text is there already)."""
        r = self.store.one("SELECT page FROM dispatch_scope WHERE incident_id = ?", (src,))
        if r is None or not r["page"]:
            return
        kind, depts, *rest = json.loads(r["page"])
        line = rest[0] if rest else ""
        feed, _ = self._reading(dst, kind, list(depts), note=False, line=line)
        text = (line + (f" · feed: {feed}" if feed else "")) if line else ""
        if not text:
            return
        with self.store.tx() as db:
            if not db.execute("SELECT 1 FROM events WHERE incident_id = ? AND kind = 'tones' AND text = ?", (dst, text)).fetchone():
                self.store._event(db, dst, "tones", text, "Paging", at=self.store.clock())

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
        now = self.store.clock()
        with self.store.tx() as db:
            r = db.execute("SELECT * FROM dispatch_scope WHERE incident_id = ?", (iid,)).fetchone()
            if r is not None and r["source"] == "feed" and (r["kind"], json.loads(r["departments"])) == (kind, depts):
                return
            db.execute("INSERT INTO dispatch_scope (incident_id, kind, departments, source, at) VALUES (?, ?, ?, 'feed', ?) "
                       "ON CONFLICT(incident_id) DO UPDATE SET kind = excluded.kind, departments = excluded.departments, "
                       "source = 'feed', at = excluded.at", (iid, kind, json.dumps(depts), now))
            if r is not None and r["source"] == "page" and (kind, depts) != (r["kind"], json.loads(r["departments"])):
                self.store._event(db, iid, "feed", f"Feed: {_said(kind, depts)} · page: "
                                  f"{_said(r['kind'], json.loads(r['departments']))}", at=now)

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
            lab = label(r["kind"], depts) if r else ""
            inc["scope"] = {"kind": r["kind"], "departments": depts, "short": short(r["kind"], depts), "label": lab,
                            "paged": f"Paged: {lab[:1].lower()}{lab[1:]}", "source": r["source"]} if r else None
        return incs
