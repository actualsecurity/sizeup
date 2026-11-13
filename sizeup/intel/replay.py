"""Replay / backtest (radio intel plan, PR 7): the intel pipeline over stored radio log rows, in memory. Nothing is
pushed and nothing is written: not incidents, radio_links, incoming, alarms, nor the intel tables themselves. The rows
and the Settings snapshot come from a connection opened read-only (open_ro); the engine gets an in-memory event store
(events.MemoryEvents), no CAD store (no meta writes) and the recording sink below (no ntfy).

It drives the intel core's own engine (sizeup/intel/engine.py), never a copy of it:

    IntelEngine(packs=, settings=, events=MemoryEvents(), sink=Recorder, locate=, clock=, thread=False, store=None)
    engine.feed(row)        one radio_traffic row, synchronously; the clock is the row's START (the policy reads it)
    engine.tick(now)        events expire, a due digest is sent to the sink
    engine.ruleset_hash()
    sink.hit / event / push(Event, Decision) and the optional clear(tid, keep) / digest(events): recorded, never done.
    Decision.message (the composed push) is dropped on arrival: it may hold police text, and replay keeps aggregates only.

Used by tools/intel_replay.py (a restored cad.sqlite, aggregates only), the private `mrrs-intel` replay tests
(tests/test_intel_replay.py) and Settings > Intel's dry run on save (PR 6).

Metrics (metrics()):
    rules / categories   hits, events, would-push, labelled, what-right (tp + near + time_only), precision
    recall               of the county CAD's incidents of a label class in the span, the share some event had first
                         or within the window (sizeup/intel/labels.py), and the lead time (median, p90; > 0: earlier)
    place                address-resolved rate (address or intersection), error median / p90 in metres, false-place
                         rate (> 300 m off, of the labelled same-call events with a point)
    pushes_per_day       would-push decisions over the span's days (digests counted apart)
    latency              the pipeline's own time per row (ms, p50 / p90 / max) and the raise delay (s: an event's
                         first raise after its earliest transmission started)
    shed                 rows in the span that speech-to-text shed (status skipped: "skipped: busy ..."): lost recall
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

from sizeup.intel import labels as lab

ROW_COLUMNS = ("id", "start", "end", "source", "system", "service", "dept", "tier", "talkgroup", "channel", "talker",
               "rig", "radio_id", "dispatcher", "text", "alt_text", "model", "alt_model", "status")
SETTINGS_SECTIONS = ("intel", "intel_packs", "intel_rules")       # the engine's (sizeup/intel/engine.py SECTIONS)
TICK_EVERY_S = 15.0                # the live thread's TICK_S
FLUSH_S = 6 * 3600.0               # after the last row: time moved on this far, every window closed, every event ended
WHAT_RIGHT = ("tp", "near", "time_only")
LABELLED = WHAT_RIGHT + ("false_place", "none")    # precision's denominator (never unlabelled, ambiguous, no_time)
SHED = "skipped: busy"             # sizeup/stt.py SHED: what a shed clip's model line says


class Pipeline(Protocol):
    """The intel core's engine as replay drives it (see the module doc); tests pass a stand-in with the same shape."""
    def feed(self, row: dict): ...
    def tick(self, now: float): ...
    def ruleset_hash(self) -> str: ...


def _d(x) -> dict:
    """An event / hit / decision as a dict, whatever it is (the core's dataclasses, a dict)."""
    if isinstance(x, dict):
        return dict(x)
    if is_dataclass(x) and not isinstance(x, type):
        return asdict(x)
    return {k: getattr(x, k) for k in dir(x) if not k.startswith("_") and not callable(getattr(x, k))}


class Recorder:
    """The replay's sink: keeps what the engine did, in memory. Never a push's message (has_message only)."""

    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.hits: list[dict] = []
        self.events: dict[str, dict] = {}         # id -> the latest snapshot (+ raised_at: the clock at its first raise)
        self.decisions: dict[str, list[dict]] = defaultdict(list)
        self.digests = 0                           # digests due (events held over a rate cap)
        self.digested = 0                          # ... the events in them

    def hit(self, hit) -> None:
        self.hits.append(_d(hit))

    def clear(self, tid: str, keep) -> None:
        """A re-transcription: the rules that no longer hit tid are forgotten (the live intel_hits does the same)."""
        keep = set(keep or ())
        self.hits = [h for h in self.hits if not (str(h.get("traffic_id")) == str(tid) and h.get("rule_id") not in keep)]

    def event(self, event, change: str) -> None:
        e = _d(event)
        eid = str(e.get("id"))
        prev = self.events.get(eid)
        e["raised_at"] = prev["raised_at"] if prev else self.clock()
        e["last_change"] = change
        self.events[eid] = e

    def push(self, event, decision) -> None:
        d = _d(decision)
        d["has_message"] = bool(d.pop("message", None))     # the composed push: never kept, never printed
        self.decisions[str(_d(event).get("id"))].append(d)

    def digest(self, events) -> None:
        self.digests += 1
        self.digested += len(list(events))

    def would_push(self, eid: str) -> bool:
        return any(d.get("would_push") for d in self.decisions.get(eid, ()))


PipelineFactory = Callable[[Recorder, "Path | None", Callable[[], float]], Pipeline]


def core_pipeline(sink: Recorder, rules: Path | None, clock: Callable[[], float], settings: dict | None = None,
                  locate=None) -> Pipeline:
    """The intel core's engine, offline: in-memory events, no store, no thread. rules: a pack file in place of
    data/intel/*.json; settings: the Settings snapshot (read_settings), its overrides on either."""
    from sizeup.intel import events, rules as rules_mod
    from sizeup.intel.engine import IntelEngine
    snap = settings or {}
    packs = rules_mod.load_packs([rules], snap) if rules else None
    return IntelEngine(packs=packs, settings=snap, events=events.MemoryEvents(), sink=sink, locate=locate, clock=clock,
                       thread=False, store=None, log=lambda m: None)


def gazetteer_locator():
    """place.Locator over the shipped gazetteer (data/gazetteer.sqlite, read only), as the live engine has it; None
    when the gazetteer can't load (events then have no place)."""
    try:
        from sizeup.geo.gazetteer import Gazetteer
        from sizeup.intel.place import Locator
        return Locator(Gazetteer())
    except Exception:  # noqa: BLE001
        return None


# -- the rows ------------------------------------------------------------------------------------------------------------

def open_ro(path: str | Path) -> sqlite3.Connection:
    """A restored cad.sqlite, read-only: SQLite refuses every write on this connection (mode=ro, query_only)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def read_settings(db: sqlite3.Connection) -> dict:
    """The copy's intel Settings ({section: {key: value}}), as the live engine reads them (Settings.section)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings_overrides'").fetchone():
        return {}
    out: dict = {s: {} for s in SETTINGS_SECTIONS}
    for sec, key, value in db.execute("SELECT section, key, value FROM settings_overrides WHERE section IN "
                                      f"({','.join('?' * len(SETTINGS_SECTIONS))})", SETTINGS_SECTIONS):
        try:
            out[sec][key] = json.loads(value)
        except ValueError:
            continue
    return out


def load_rows(db: sqlite3.Connection, since: float | None = None, until: float | None = None,
              services: Iterable[str] = ()) -> list[dict]:
    have = {r[1] for r in db.execute("PRAGMA table_info(radio_traffic)")}
    cols = ", ".join(c if c in have else f"'' AS {c}" for c in ROW_COLUMNS)
    where, args = ["1"], []
    if since is not None:
        where.append("start >= ?")
        args.append(since)
    if until is not None:
        where.append("start < ?")
        args.append(until)
    svc = [s for s in services if s]
    if svc:
        where.append(f"service IN ({','.join('?' * len(svc))})")
        args += svc
    return [dict(r) for r in db.execute(f"SELECT {cols} FROM radio_traffic WHERE {' AND '.join(where)} "
                                        "ORDER BY start, id", args)]


# -- the run -------------------------------------------------------------------------------------------------------------

class Run:
    def __init__(self, rows: list[dict], rec: Recorder, feed_s: list[float], ruleset: str):
        self.rows, self.rec, self.feed_s, self.ruleset = rows, rec, feed_s, ruleset


def replay(rows: list[dict], factory: PipelineFactory | None = None, rules: Path | None = None,
           settings: dict | None = None, locate=None) -> Run:
    """Every row through the engine in time order, the clock at each row's START (the policy decides by clock()),
    ticked every TICK_EVERY_S of row time, then flushed FLUSH_S on. factory: the intel core's engine unless given."""
    if factory is None:
        def factory(sink, r, clock):
            return core_pipeline(sink, r, clock, settings, locate)
    now = [float(rows[0]["start"]) if rows else time.time()]
    rec = Recorder(lambda: now[0])
    p = factory(rec, rules, lambda: now[0])
    feed_s: list[float] = []
    last_tick = now[0]
    for r in rows:
        now[0] = max(now[0], float(r.get("start") or now[0]))
        if now[0] - last_tick >= TICK_EVERY_S:
            p.tick(now[0])
            last_tick = now[0]
        t0 = time.perf_counter()
        p.feed(r)
        feed_s.append(time.perf_counter() - t0)
    now[0] += FLUSH_S
    p.tick(now[0])
    try:
        rs = p.ruleset_hash()
    except Exception:  # noqa: BLE001
        rs = ""
    return Run(rows, rec, feed_s, rs)


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))]


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def metrics(run: Run, incidents: list[dict] | None = None, categories: dict | None = None) -> dict:
    """The run's aggregates (see the module doc). incidents: the county's (labels.county_incidents), None: no labels."""
    rows, rec = run.rows, run.rec
    span = (rows[-1]["start"] - rows[0]["start"]) if len(rows) > 1 else 0.0
    days = max(span / 86400.0, 1.0 / 24)
    starts = {r["id"]: float(r["start"]) for r in rows}
    evs = list(rec.events.values())
    labelled = {x["id"]: x for x in lab.match(evs, incidents, categories)} if incidents is not None else {}
    per_rule: dict[str, dict] = defaultdict(lambda: {"hits": 0, "events": 0, "pushes": 0, "labelled": 0, "right": 0})
    per_cat: dict[str, dict] = defaultdict(lambda: {"events": 0, "pushes": 0, "labelled": 0, "right": 0, "tp": 0,
                                                    "false_place": 0})
    for h in rec.hits:
        per_rule[str(h.get("rule_id"))]["hits"] += 1
    resolved = placed = 0
    errors: list[float] = []
    delays: list[float] = []
    same_call_placed = false_place = 0
    for e in evs:
        eid = str(e.get("id"))
        pushed = rec.would_push(eid)
        lb = labelled.get(e.get("id"), {})
        label = lb.get("label")
        rule_ids = {str(r[0] if isinstance(r, (list, tuple)) else r) for r in (e.get("rules") or [])}
        c = per_cat[str(e.get("category"))]
        c["events"] += 1
        c["pushes"] += pushed
        for rid in rule_ids:
            per_rule[rid]["events"] += 1
            per_rule[rid]["pushes"] += pushed
        if label in LABELLED:
            c["labelled"] += 1
            c["right"] += label in WHAT_RIGHT
            c["tp"] += label == "tp"
            c["false_place"] += label == "false_place"
            for rid in rule_ids:
                per_rule[rid]["labelled"] += 1
                per_rule[rid]["right"] += label in WHAT_RIGHT
            if lb.get("error_m") is not None:
                errors.append(lb["error_m"])
                same_call_placed += 1
                false_place += label == "false_place"
        prec = e.get("precision") or ""
        if prec and prec != "none":
            placed += 1
            resolved += prec in ("address", "intersection")
        tids = [t for t in (e.get("traffic_ids") or []) if t in starts]
        if tids and e.get("raised_at") is not None:
            delays.append(max(0.0, float(e["raised_at"]) - min(starts[t] for t in tids)))
    for d in list(per_rule.values()) + list(per_cat.values()):
        d["precision"] = _rate(d["right"], d["labelled"])
    recall = None
    if incidents is not None:
        lo, hi = (rows[0]["start"], rows[-1]["end"]) if rows else (0.0, 0.0)
        inside = [k for k, x in enumerate(incidents) if lo <= x["t"] <= hi]
        firsts: dict[int, float] = {}
        for x in labelled.values():
            k = x.get("incident")
            if k is not None and x["label"] in WHAT_RIGHT and x["lead_s"] is not None:
                firsts[k] = max(firsts.get(k, -1e18), x["lead_s"])      # the earliest event's lead
        leads = [firsts[k] for k in inside if k in firsts]
        recall = {"county_incidents": len(inside), "matched": len(leads), "recall": _rate(len(leads), len(inside)),
                  "lead_s_median": _pct(leads, 0.5), "lead_s_p90": _pct(leads, 0.9),
                  "by_class": {cls: {"n": sum(1 for k in inside if incidents[k]["cls"] == cls),
                                     "matched": sum(1 for k in inside if incidents[k]["cls"] == cls and k in firsts)}
                               for cls in sorted({incidents[k]["cls"] for k in inside})}}
    ms = [s * 1000 for s in run.feed_s]
    shed = sum(1 for r in rows if r.get("status") == "skipped" and SHED in (r.get("model") or ""))
    return {"rows": len(rows), "days": round(days, 2), "ruleset": run.ruleset, "hits": len(rec.hits), "events": len(evs),
            "pushes": sum(rec.would_push(str(e.get("id"))) for e in evs),
            "pushes_per_day": round(sum(rec.would_push(str(e.get("id"))) for e in evs) / days, 2),
            "rules": {k: dict(v) for k, v in sorted(per_rule.items())},
            "categories": {k: dict(v) for k, v in sorted(per_cat.items())},
            "recall": recall,
            "place": {"placed": placed, "address_resolved": _rate(resolved, placed),
                      "error_m_median": _pct(errors, 0.5), "error_m_p90": _pct(errors, 0.9),
                      "false_place": _rate(false_place, same_call_placed)},
            "latency": {"feed_ms_p50": _round(_pct(ms, 0.5)), "feed_ms_p90": _round(_pct(ms, 0.9)),
                        "feed_ms_max": _round(max(ms) if ms else None),
                        "raise_s_median": _pct(delays, 0.5), "raise_s_p90": _pct(delays, 0.9)},
            "digests": rec.digests, "shed": shed}


def _round(x):
    return None if x is None else round(x, 3)


def _key(e: dict) -> tuple:
    """An event's identity across two rule sets: its category and its earliest transmission."""
    tids = sorted(e.get("traffic_ids") or [])
    return (str(e.get("category")), tids[0] if tids else str(e.get("subject")))


def event_keys(run: Run, pushed: bool = False) -> set[tuple]:
    return {_key(e) for e in run.rec.events.values() if not pushed or run.rec.would_push(str(e.get("id")))}


def diff(a: Run, b: Run, ma: dict, mb: dict) -> dict:
    """B against A over the same rows: events / pushes gained and lost (counts), and the headline metrics side by side."""
    ka, kb = event_keys(a), event_keys(b)
    pa, pb = event_keys(a, pushed=True), event_keys(b, pushed=True)

    def head(m):
        r = m.get("recall") or {}
        return {"events": m["events"], "pushes_per_day": m["pushes_per_day"], "recall": r.get("recall"),
                "lead_s_median": r.get("lead_s_median"), "false_place": m["place"]["false_place"],
                "address_resolved": m["place"]["address_resolved"]}
    return {"events_gained": len(kb - ka), "events_lost": len(ka - kb), "pushes_gained": len(pb - pa),
            "pushes_lost": len(pa - pb), "a": head(ma), "b": head(mb)}
