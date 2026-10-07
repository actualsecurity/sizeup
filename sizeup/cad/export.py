"""Records out of the CAD as CSV, with the old sheet's exact columns and codes, so the department
can still open them in a spreadsheet next to "[TEMPLATE] PCFD ECC OPS":

    incidents.csv      the sheet's Incident Database tab (every numbered event)
    calls.csv          Call History (closed events)
    unit-history.csv   Unit History (every unit status change)
    event-log.csv      Event Log (every chronology entry, and unit changes off any event)

and admin(): record counts and the next event number (the sheet's CAD ADMIN tab, read-only:
there is no "clear all data" here on purpose).
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from sizeup.cad.store import TZ, Store, event_number, local_year

INCIDENT_COLUMNS = ["Incident #", "Unused", "Event Type", "Event Sub-Type", "Location", "Municipality", "Received",
                    "Status", "Assigned Units", "Dispatch", "First Enroute", "First On Scene", "Clear", "Duration",
                    "Disposition", "Cross Streets", "Narrative Summary", "Closed?"]
CALL_COLUMNS = ["Incident #", "Event Type", "Event Sub-Type", "Location", "Received", "Clear", "Duration",
                "Disposition", "Units"]
UNIT_HISTORY_COLUMNS = ["Timestamp", "Unit", "Old Status", "New Status", "Incident #", "Operator", "Note"]
EVENT_LOG_COLUMNS = ["Timestamp", "Incident #", "Unit", "Event Type", "Event / Comment", "Operator"]
# Chronology kinds -> the sheet's Event Log "Event Type" (SYSTEM | EVENT | UNIT STATUS | ...)
LOG_TYPES = {"system": "SYSTEM", "note": "COMMENT", "dispatch": "DISPATCH", "attach": "DISPATCH"}
FILES = {"incidents.csv", "calls.csv", "unit-history.csv", "event-log.csv"}


def ts(at: float | None) -> str:
    return datetime.fromtimestamp(at, TZ).strftime("%m/%d/%Y %H:%M:%S") if at else ""


def hms(s: float | None) -> str:
    if s is None or s < 0:
        return ""
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def municipality(geo_label: str) -> str:
    """'209 Westchester Ave, Port Chester' -> 'Port Chester' (the gazetteer's label ends with the town)."""
    parts = [p.strip() for p in (geo_label or "").split(",")]
    return parts[-1] if len(parts) > 1 else ""


def _csv(columns: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(columns)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")       # BOM: Excel opens it as UTF-8


def _incidents(store: Store, closed_only: bool) -> list[dict]:
    rows = store.q("SELECT * FROM incidents WHERE number IS NOT NULL" + (" AND status = 'closed'" if closed_only else "")
                   + " ORDER BY created_at, id")
    if not rows:
        return rows
    times = {r["incident_id"]: r for r in store.q(
        "SELECT incident_id, MIN(dispatched_at) AS dispatched, MIN(enroute_at) AS enroute, MIN(onscene_at) AS onscene "
        "FROM assignments GROUP BY incident_id")}
    units: dict[int, list[str]] = {}
    for a in store.q("SELECT a.incident_id, u.name FROM assignments a JOIN units u ON u.id = a.unit_id ORDER BY a.id"):
        lst = units.setdefault(a["incident_id"], [])
        if a["name"] not in lst:
            lst.append(a["name"])
    for r in rows:
        t = times.get(r["id"], {})
        r["t_dispatch"], r["t_enroute"], r["t_onscene"] = t.get("dispatched"), t.get("enroute"), t.get("onscene")
        r["units"] = units.get(r["id"], [])
        r["duration"] = hms(r["closed_at"] - r["created_at"]) if r["closed_at"] else ""
    return rows


def incidents_csv(store: Store) -> bytes:
    rows = [[r["number"], "", r["type"], r["subtype"], r["address"], municipality(r["geo_label"]), ts(r["created_at"]),
             r["status"].upper(), ", ".join(r["units"]), ts(r["t_dispatch"]), ts(r["t_enroute"]), ts(r["t_onscene"]),
             ts(r["closed_at"]), r["duration"], r["disposition"], r["cross_street"], r["remarks"],
             "TRUE" if r["status"] == "closed" else "FALSE"]
            for r in _incidents(store, closed_only=False)]
    return _csv(INCIDENT_COLUMNS, rows)


def calls_csv(store: Store) -> bytes:
    rows = [[r["number"], r["type"], r["subtype"], r["address"], ts(r["created_at"]), ts(r["closed_at"]), r["duration"],
             r["disposition"], ", ".join(r["units"])] for r in _incidents(store, closed_only=True)]
    return _csv(CALL_COLUMNS, rows)


def unit_history_csv(store: Store) -> bytes:
    rows = store.q("SELECT h.*, u.name, i.number FROM unit_history h JOIN units u ON u.id = h.unit_id "
                   "LEFT JOIN incidents i ON i.id = h.incident_id ORDER BY h.at, h.id")
    return _csv(UNIT_HISTORY_COLUMNS, [[ts(r["at"]), r["name"], r["old_status"], r["new_status"], r["number"] or "",
                                        r["operator"], r["note"]] for r in rows])


def event_log_csv(store: Store) -> bytes:
    rows = store.q("SELECT e.*, u.name, i.number FROM events e LEFT JOIN units u ON u.id = e.unit_id "
                   "LEFT JOIN incidents i ON i.id = e.incident_id ORDER BY e.at, e.id")
    out = []
    for r in rows:
        kind = LOG_TYPES.get(r["kind"]) or ("UNIT STATUS" if r["kind"] == "status" and r["unit_id"] else "EVENT")
        unit = ", ".join(x for x in (r["units"] or "").split(",") if x) or r["name"] or ""   # a note's unit tags
        out.append([ts(r["at"]), r["number"] or "", unit, kind, r["text"], r["operator"]])
    return _csv(EVENT_LOG_COLUMNS, out)


def export(store: Store, name: str) -> bytes | None:
    return {"incidents.csv": incidents_csv, "calls.csv": calls_csv, "unit-history.csv": unit_history_csv,
            "event-log.csv": event_log_csv}.get(name, lambda s: None)(store)


def admin(store: Store) -> dict:
    """Record counts per sheet tab and the next event number (what ACCEPT EVENT would get now)."""
    one = lambda sql: store.one(sql)["n"]
    year = local_year(store.clock())
    last = store.one("SELECT last FROM seq WHERE year = ?", (year,))
    return {
        "counts": {
            "Incident database": one("SELECT COUNT(*) AS n FROM incidents WHERE number IS NOT NULL"),
            "Open events": one("SELECT COUNT(*) AS n FROM incidents WHERE status IN ('pending', 'active')"),
            "Waiting to be accepted": one("SELECT COUNT(*) AS n FROM incidents WHERE status = 'queued'"),
            "Call history": one("SELECT COUNT(*) AS n FROM incidents WHERE status = 'closed'"),
            "Unit history": one("SELECT COUNT(*) AS n FROM unit_history"),
            "Event log": one("SELECT COUNT(*) AS n FROM events"),
            "Dispatches": one("SELECT COUNT(*) AS n FROM dispatches"),
            "Units": one("SELECT COUNT(*) AS n FROM units"),
        },
        "next_number": event_number(year, (last["last"] if last else 0) + 1),
        "db": store.path,
    }
