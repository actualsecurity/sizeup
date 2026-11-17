"""Golden characterisation of today's airport pre-alerts (the pipeline migration's PR 1): tests only, no code changed.

Invented airband and fire-radio scenarios (tests/fixtures/pipeline/airband/*.jsonl) run through the real entry points of
a real App (App._transcribed / App._retext -> App._atc -> AtcWatch, AtcTalkers, AirportWatch; Cad._on_text ->
AirportWatch.heard_radio; App._atc_view -> AirportWatch.tick; the console's accept / dismiss / state) with:

    an injected clock       the Cad's (and so its AirportAlerts, AirportWatch, AtcTalkers, Sources, Store) and the
                            AtcWatch's: every time in a golden is seconds from the scenario's t0
    a fake Aircraft layer   what App._atc_aircraft reads (view, wide, st); a picture's age follows the scenario's clock
    a recording publisher   sizeup.notify.publish replaced: every push that reached ntfy, with the KIND it went to (the
                            topic it was sent to, named by role: "alerts", "status" or "intel"; never a topic's name)
    a recording poll-now    Cad.sources.trigger (the station's sources_poll_now): the county feed's poke reasons
    spies                   AtcWatch.heard / Rules.hits / AtcWatch.confidence, AtcTalkers.heard, AirportWatch.heard_atc /
                            heard_radio, AirportAlerts.raise_alert / add_to and the three watches' log lines: what was
                            decided and why, recorded around the real calls (never instead of them)

After every step the harness records what changed: the airport_alerts rows (read raw, without the lazy expiry a console
read would run), new or changed incidents, new CAD event-log lines, pushes, pokes and the decision trace. Row and
incident ids are named by order of appearance in the scenario (A1, A2 ...; I1 ...).

Steps (one JSON object per line; "t" is seconds from the scenario's t0, and the clock is set to it first):

    {"do": "scenario", "t0": "day" | "night", "levels": [2, 3], "atc_enabled": true, "why": "..."}   first line, optional
    {"do": "picture", "t": 0, "planes": [{...}], "wide": [{...}]}   the Aircraft layer's picture (App._atc_view)
    {"do": "air", "t": 10, "id": "twr-1", "ch": "HPN Tower", "dur": 4, "ago": 0, "text": "..."}
                                    a new airband transmission ending `ago` s before t (App._transcribed)
    {"do": "retext", "t": 20, "id": "twr-1", "text": "..."}        new text for one (App._retext, a re-run)
    {"do": "adsb", "t": 25, "what": "start" | "update" | "again" | "end", "ev": {...}, "why": "..."}
                                    an aircraft emergency from the Aircraft layer's watch (Rig._adsb)
    {"do": "radio", "t": 30, "id": "fire-1", "tg": 307, "text": "..."}   a fire-radio transmission (App._transcribed)
    {"do": "accept" | "dismiss", "t": 40, "alert": "A1", "operator": "...", "reason": "..."}   the console's answer
    {"do": "read", "t": 50}         the console reads the CAD state (the lazy expiry runs here)
    {"do": "wait", "t": 60}         the clock moves, nothing else

rules.jsonl holds one case per line ({"rule", "expect": "fires" | "quiet", "why", "steps"}), all run in one App, each
CASE_GAP_S after the last with the previous case's pre-alerts expired first, so no case sees another's.

Regenerate the goldens deliberately (never on a test run), then read the diff before committing it:

    .venv/bin/python -m tests.airport_golden --write              # every scenario
    .venv/bin/python -m tests.airport_golden --write rules hourly_cap
    .venv/bin/python -m tests.airport_golden                      # just show what differs
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "pipeline" / "airband"
NIGHT = 1_800_000_000.0           # 03:00 local (America/New_York): the tower closed
DAY = NIGHT + 9 * 3600            # 12:00 local
CASE_GAP_S = 3 * 3600.0           # between two rule cases: past the hourly cap, the expiry, every watch's memory
# invented topics, one per kind, so where a push went is known by its role (never a real topic's name in the repo)
TOPICS = {"alerts": "golden-test-alerts-topic", "status": "golden-test-status-topic", "intel": "golden-test-intel-topic"}
PUBLIC_URL = "https://cad.example.test"
NTFY = "https://ntfy.example.invalid"      # never reached (publish is replaced); a name that can't resolve if it were
FIRE_TG = 307                     # a fire talkgroup 60 Control is heard on (invented label below)


def channels():
    from sizeup.config import ChannelCfg
    return (ChannelCfg("HPN Tower", 118.575, "airband"),
            ChannelCfg("NY App 126.4/120.8", 126.4, "airband", schedule=((420, 126.4), (1380, 120.8)),
                       also=((124.65, 420, 1380, "NY App 124.65"),)),
            ChannelCfg("NY App 124.65", 124.65, "airband"),
            ChannelCfg("HPN Ground", 121.825, "airband"),
            ChannelCfg("FG1", 453.0375, "traffic"))


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeLayer:
    """The Aircraft layer as App._atc_aircraft and App._atc read it (st, view, wide, _lock, widen). Its picture's
    "fetched" is set before every step so the App's own freshness check (60 s) sees the scenario's age."""

    def __init__(self):
        self.view: list[dict] = []
        self.wide: list[dict] = []
        self._lock = threading.Lock()
        self.st: dict = {}
        self.at: float | None = None
        self.widened: list[float] = []

    def widen(self, nm: float) -> None:
        self.widened.append(nm)

    def age_to(self, now: float) -> None:
        self.st = {} if self.at is None else {"at": self.at, "fetched": time.time() - (now - self.at)}


class Ids:
    """Rows and incidents named by order of appearance: A1, A2 ...; I1 ..."""

    def __init__(self):
        self.a: dict[int, str] = {}
        self.i: dict[int, str] = {}

    def alert(self, pid) -> str | None:
        if pid is None:
            return None
        return self.a.setdefault(int(pid), f"A{len(self.a) + 1}")

    def incident(self, iid) -> str | None:
        if iid is None:
            return None
        return self.i.setdefault(int(iid), f"I{len(self.i) + 1}")

    def text(self, s: str) -> str:
        """Ids inside text (log lines, a source_ref): '#12' and 'alert-level:12' by their names."""
        s = re.sub(r"#(\d+)\b", lambda m: "#" + (self.a.get(int(m.group(1))) or m.group(1)), s)
        s = re.sub(r"alert-level:(\d+)", lambda m: "alert-level:" + (self.a.get(int(m.group(1))) or m.group(1)), s)
        return re.sub(r"\bevent (\d+)\b", lambda m: "event " + (self.i.get(int(m.group(1))) or m.group(1)), s)


class Rig:
    """One App wired as production wires it, with the fakes above. Use as a context manager."""

    INCIDENT_COLS = ("number", "type", "subtype", "status", "source", "source_ref", "emergency", "received_via",
                     "address", "lat", "lon", "geo_label", "remarks", "acft_type", "acft_nature", "acft_runway",
                     "created_at", "accepted_at")

    def __init__(self, tmp: Path, t0: float = DAY, levels=(2, 3), atc_enabled: bool = True):
        from sizeup.app import App
        from sizeup.cad.service import Cad
        from tests.test_app import cfg
        self.base = t0
        self.clock = Clock(t0)
        self.ids = Ids()
        self.trace: list[dict] = []
        self.pushes: list[dict] = []
        self.pokes: list[str] = []
        self.logs: list[str] = []
        self._patches = [mock.patch("sizeup.notify.publish", self._publish)]
        orig, rig_clock = Cad.__init__, self.clock

        def init(cad, s, geocoder=None, log=print, clock=None):     # the App's Cad, on the scenario's clock
            orig(cad, s, geocoder=geocoder, log=log, clock=rig_clock)
        c = replace(cfg(tmp), channels=channels(), alerts_ntfy_topic=TOPICS["alerts"], alerts_status_topic=TOPICS["status"],
                    alerts_intel_topic=TOPICS["intel"], alerts_ntfy_server=NTFY, cloud_public_url=PUBLIC_URL,
                    p25_talkgroups={FIRE_TG: "Fire/EMS 15"}, p25_incident_talkgroups=frozenset({FIRE_TG}),
                    atc_alert_level_incident=tuple(levels), atc_enabled=atc_enabled)
        with mock.patch.object(Cad, "__init__", init):
            self.app = App(c)
        app = self.app
        assert app.cad.clock is self.clock and (app.atc is not None) == atc_enabled
        if app.atc is not None:
            app.atc.clock = self.clock                # the one airport clock the App doesn't hand down (time.time)
        self.adsb = app.aircraft                      # the real layer: its emergency watch's glue (adsb steps)
        self.alarm_ids: dict[str, object] = {}
        self.layer = FakeLayer()
        app.aircraft = self.layer
        app.cad.sources.trigger = self.pokes.append    # the station's sources_poll_now
        self._spy()

    # -- context ----------------------------------------------------------------------------------------------------

    def __enter__(self) -> Rig:
        for p in self._patches:
            p.start()
        self._baseline()
        return self

    def __exit__(self, *exc) -> None:
        self._flush()
        for p in reversed(self._patches):
            p.stop()
        try:
            self.app.cad.shutdown()
        except Exception:  # noqa: BLE001 - a test's App: nothing to keep
            pass

    # -- the fakes and spies ----------------------------------------------------------------------------------------

    def _publish(self, server, topic, title, body, priority=3, tags=(), click="", timeout=None, who=None, actions=(),
                 protected=()):
        role = next((k for k, v in TOPICS.items() if v == topic), "other")
        self.pushes.append({"to": role, "title": title, "body": body, "priority": priority, "tags": list(tags),
                            "click": click, "who": who.as_dict() if who is not None else None,
                            "actions": [dict(a) for a in actions or ()]})
        return {"id": f"push-{len(self.pushes)}"}

    def _spy(self) -> None:
        app, rig = self.app, self
        watch, aw, aa, talkers = app.atc, app.cad.airport_watch, app.cad.airport_alerts, app.cad.atc_talkers
        if watch is not None:
            self._spy_atc(watch)

        t_heard = talkers.heard

        def k_heard(t):
            rec = t_heard(t)
            rig.trace.append({"watch": "talkers", "id": t.get("id"), "record": talker(rec)})
            return rec
        talkers.heard = k_heard

        a_atc, a_radio = aw.heard_atc, aw.heard_radio

        def aw_atc(t, rec=None):
            row = a_atc(t, rec)
            rig.trace.append({"watch": "airport_watch", "id": t.get("id"),
                              "row": rig.ids.alert(row["id"]) if row else None})
            return row

        def aw_radio(t):
            row = a_radio(t)
            if t.get("channel") not in aw.airband:    # (Cad._on_text hands it every row; an airband one is never its)
                rig.trace.append({"watch": "60 Control", "id": t.get("id"),
                                  "row": rig.ids.alert(row["id"]) if row else None})
            return row
        aw.heard_atc, aw.heard_radio = aw_atc, aw_radio

        raise_alert, add_to = aa.raise_alert, aa.add_to

        def aa_raise(a):
            row, new = raise_alert(a)
            rig.trace.append({"raise": alert_in(a), "row": rig.ids.alert(row["id"]) if row else None,
                              "how": ("new" if new else "added") if row else "dropped"})
            return row, new

        def aa_add(pid, a):
            row = add_to(pid, a)
            rig.trace.append({"add_to": rig.ids.alert(pid), "alert": alert_in(a),
                              "row": rig.ids.alert(row["id"]) if row else None})
            return row
        aa.raise_alert, aa.add_to = aa_raise, aa_add

        for obj, name in ((aa, "airport_alerts"), (watch, "atc"), (aw, "airport_watch")):
            if obj is not None:
                obj.log = (lambda n: lambda m: rig.logs.append(f"{n}: {m}"))(name)

    def _spy_atc(self, watch) -> None:
        """AtcWatch.heard with what its rules hit, the exchange it read and the score (Rules.hits, confidence)."""
        rig, rules, cur = self, watch.rules, {}
        heard, hits, confidence = watch.heard, rules.hits, watch.confidence

        def w_heard(t):
            cur.clear()
            cur.update({"watch": "atc", "id": t.get("id"), "ch": t.get("source"), "fed": t.get("text"),
                        "hits": None})
            rig.trace.append(cur.copy())
            entry = rig.trace[-1]
            try:
                row = heard(t)
            except Exception as e:  # noqa: BLE001
                entry["error"] = f"{type(e).__name__}: {e}"
                raise
            entry.update({k: v for k, v in cur.items() if k in ("hits", "context", "score", "adsb")})
            entry["row"] = rig.ids.alert(row["id"]) if row else None
            return row

        def r_hits(norm):
            out = hits(norm)
            cur["hits"] = [[h.rule.id, h.text] for h in out]
            return out

        def w_conf(me, ctx):
            c, adsb = confidence(me, ctx)
            cur["score"], cur["adsb"] = round(c, 6), list(adsb) if adsb else None
            cur["context"] = [x.id for x in ctx]
            return c, adsb
        watch.heard, rules.hits, watch.confidence = w_heard, r_hits, w_conf

    # -- snapshots --------------------------------------------------------------------------------------------------

    def rel(self, x):
        """Every epoch time as seconds from t0 (anything else as it is), all the way down."""
        if isinstance(x, dict):
            return {k: self.rel(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [self.rel(v) for v in x]
        if isinstance(x, float) and x > 1e9:
            return round(x - self.base, 3)
        return x

    def _rows(self) -> dict[int, dict]:
        return {r["id"]: r for r in self.app.cad.airport_alerts._rows("1 = 1", (), 100000)}   # raw: no lazy expiry

    def _incidents(self) -> dict[int, dict]:
        cols = ", ".join(("id",) + self.INCIDENT_COLS)
        return {r["id"]: r for r in self.app.cad.store.q(f"SELECT {cols} FROM incidents")}

    def _baseline(self) -> None:
        self._flush()
        self.rows, self.incs = self._rows(), self._incidents()
        self.last_event = (self.app.cad.store.one("SELECT MAX(id) AS n FROM events") or {}).get("n") or 0
        self.trace.clear()
        self.pushes.clear()
        self.pokes.clear()
        self.logs.clear()
        self.layer.widened.clear()

    def _flush(self) -> None:
        for n in self.app.pushes.made():
            n.flush(10.0)

    def row(self, r: dict) -> dict:
        ids = self.ids
        return {"id": ids.alert(r["id"]), "source": r["source"], "kind": r["kind"], "subject": r["subject"],
                "group": r["group"], "at": self.rel(r["at"]), "updated_at": self.rel(r["updated_at"]), "title": r["title"],
                "detail": r["detail"], "confidence": r["confidence"], "level": r["level"], "channel": r["channel"],
                "count": r["count"], "status": r["status"], "outcome_at": self.rel(r["outcome_at"]),
                "incident": ids.incident(r["incident_id"]), "operator": r["operator"], "note": r["note"],
                "data": self.rel(r["data"])}

    def incident(self, r: dict) -> dict:
        out = {"id": self.ids.incident(r["id"])}
        for k in self.INCIDENT_COLS:
            v = r[k]
            out[k] = self.ids.text(v) if k == "source_ref" and v else self.rel(v)
        return out

    def observe(self, step: dict) -> dict:
        """What the step changed (the module doc)."""
        self._flush()
        rows, incs = self._rows(), self._incidents()
        out: dict = {"step": step}
        changed = [self.row(r) for pid, r in sorted(rows.items()) if self.rows.get(pid) != r]
        new_incs = [self.incident(r) for iid, r in sorted(incs.items()) if self.incs.get(iid) != r]
        events = self.app.cad.store.q("SELECT id, incident_id, at, kind, text, operator FROM events WHERE id > ? "
                                      "ORDER BY id", (self.last_event,))
        if self.trace:
            out["decisions"] = self.rel(self.trace)
        if changed:
            out["alerts"] = changed
        if new_incs:
            out["incidents"] = new_incs
        if events:
            out["cad_log"] = [{"incident": self.ids.incident(e["incident_id"]), "at": self.rel(e["at"]),
                               "kind": e["kind"], "text": self.ids.text(e["text"]), "operator": e["operator"]}
                              for e in events]
        if self.pushes:
            out["pushes"] = list(self.pushes)
        if self.pokes:
            out["county_feed_poll_now"] = list(self.pokes)
        if self.layer.widened:
            out["aircraft_widened_nm"] = list(self.layer.widened)
        if self.logs:
            out["log"] = [self.ids.text(x) for x in self.logs]
        self._baseline()
        return out

    # -- steps ------------------------------------------------------------------------------------------------------

    def at(self, t: float) -> float:
        self.clock.t = self.base + float(t)
        self.layer.age_to(self.clock.t)
        return self.clock.t

    def step(self, s: dict) -> dict:
        from sizeup.incidents import Traffic
        app = self.app
        do = s["do"]
        now = self.at(float(s.get("t", 0)))
        if do == "picture":
            self.layer.view = [dict(p) for p in s.get("planes") or []]
            self.layer.wide = [dict(p) for p in s.get("wide") or []]
            self.layer.at = now
            self.layer.age_to(now)
            app._atc_view(list(self.layer.view), now)
        elif do in ("air", "radio"):
            end = now - float(s.get("ago", 0))
            src = s.get("ch", "HPN Tower") if do == "air" else "Fire/EMS 15"
            tg = None if do == "air" else int(s.get("tg", FIRE_TG))
            t = Traffic(s["id"], src, end - float(s.get("dur", 4)), end, "", talkgroup=tg)
            t.raw = s["text"]                                    # type: ignore[attr-defined]
            app._transcribed(t)
        elif do == "retext":
            app._retext(s["id"], s["text"], "done", 0.9, "golden")
        elif do == "adsb":
            self._adsb(s, now)
        elif do in ("accept", "dismiss"):
            pid = next(k for k, v in self.ids.a.items() if v == s["alert"])
            body = {k: s[k] for k in ("operator", "reason") if k in s}
            try:
                r = app.cad.airport_alert(pid, do, body)
                self.trace.append({"console": do, "alert": s["alert"], "status": r["status"],
                                   "acknowledged": bool(r.get("acknowledged")),
                                   "incident": self.ids.incident((r.get("incident") or {}).get("id"))})
            except Exception as e:  # noqa: BLE001 - what the console would show
                self.trace.append({"console": do, "alert": s["alert"], "error": f"{type(e).__name__}: {e}"})
        elif do == "read":
            st = app.cad.state()["airport_alerts"]
            self.trace.append({"console": "read", "open": [self.ids.alert(r["id"]) for r in st["open"]]})
        elif do != "wait":
            raise ValueError(f"step: unknown do {do!r}")
        return self.observe(dict(s))

    def _adsb(self, s: dict, now: float) -> None:
        """An aircraft emergency as the Aircraft layer's watch (AircraftLayer._watch) hands it on: start = the alarm
        (App's on_emergency -> Cad.alarms.on_aircraft) then the pre-alert with the alarm's id; update / again / end =
        AircraftLayer._prealert (end: the alarm cleared first, App's on_clear). The watch's own timing (REALARM_S, its
        hourly alarm cap) is the layer's and isn't driven here."""
        ev = dict(s["ev"], at=now)
        if "pos_at" in ev:
            ev["pos_at"] = self.base + float(ev["pos_at"])
        key, layer, what = f"{ev.get('hex')}:{ev.get('code')}", self.adsb, s["what"]
        if what == "start":
            aid = layer.on_emergency(ev)
            self.alarm_ids[key] = aid
            self.trace.append({"adsb": "start", "alarm": aid is not None})
            if aid is not None:
                layer._prealert("start", ev, aid)
        elif what == "end":
            aid = self.alarm_ids.pop(key, None)
            if aid is not None:
                layer.on_clear(ev, aid, s["why"])
            layer._prealert("end", ev, why=s["why"])
        else:
            layer._prealert(what, ev)

    def wiring(self) -> dict:
        """How the App put it together: the numbers each decision uses, and where each kind of push goes (by role)."""
        from sizeup import airport_watch as W
        from sizeup import atc
        from sizeup import notify
        from sizeup.cad import airport_alerts as AA
        app = self.app
        aa, w, aw = app.cad.airport_alerts, app.atc, app.cad.airport_watch
        role = {v: k for k, v in TOPICS.items()}

        def kind_of(n):
            return {"kind": getattr(n, "kind", None), "to": role.get(getattr(n, "topic", ""), "none")}
        r = w.rules
        return {
            "atc": {"channels": sorted(w.channels), "max_age_s": atc.MAX_AGE_S, "context": w.context,
                    "context_s": w.context_s,
                    "threshold": r.threshold, "context_factor": r.context_factor, "routine_factor": r.routine_factor,
                    "adsb_normal": atc.ADSB_NORMAL, "adsb_worse": atc.ADSB_WORSE, "adsb_match": atc.ADSB_MATCH,
                    "rules": [{"id": x.id, "category": x.category, "weight": x.weight, "always": x.always,
                               "routine": x.routine, "not": x.never is not None, "event_type": x.event_type}
                              for x in r.rules],
                    "categories": r.categories},
            "airport_alerts": {"dedupe_s": aa.dedupe_s, "group_s": AA.GROUP_S, "max_per_hour": aa.max_per_hour,
                               "expire_s": aa.expire_s, "keep_s": AA.KEEP_S, "list_s": AA.LIST_S,
                               "max_snippets": AA.MAX_SNIPPETS, "max_aircraft": AA.MAX_AIRCRAFT, "max_phrases": AA.MAX_PHRASES,
                               "levels": [list(x) for x in AA.LEVELS], "sources": AA.SOURCES, "adsb_reopen_s": AA.REOPEN_S,
                               "public_url": aa.public_url, "notifier": kind_of(aa.notifier),
                               "notifier_is_the_alerts_kind": aa.notifier is app.pushes.notifier(notify.ALERTS)},
            "airport_watch": {"enabled": aw.enabled, "airband": sorted(aw.airband),
                              "incident_levels": sorted(aw.incident_levels), "fire_talkgroups": sorted(aw.fire_talkgroups),
                              "airport_names": list(aw.airport_names),
                              **{k: getattr(W, k) for k in WATCH_CONSTANTS}},
            "talkers": {"channels": sorted(app.cad.atc_talkers.channels), "identify": app.cad.atc_talkers.identify},
            "intel_notifier": kind_of(app.cad.intel.notifier),
            "alarms_notifier": kind_of(app.cad.alarms.notifier),
        }


WATCH_CONSTANTS = ("WINDOW_S", "EXCHANGE_S", "EXCHANGE_ABN_S", "ORPHAN_S", "STALE_S", "KEEP_S", "ADSB_ARRIVING_S", "LOST_S",
                   "LEAVE_NEAR_NM", "LEAVE_NM", "LANDED_NM", "LEVEL_KEEP_S", "ATTACH_S", "RETURN_S", "REPUSH_S")


def talker(rec: dict | None) -> dict | None:
    """The talker record as the airport watch reads it (aircraft, exchange) and who was talking."""
    if not rec:
        return None
    out = {"role": rec.get("role")}
    for k in ("talker", "aircraft", "addressed"):
        v = rec.get(k)
        if isinstance(v, dict):
            out[k] = {x: v.get(x) for x in ("callsign", "hex", "status") if v.get(x) is not None}
    ex = rec.get("exchange")
    if isinstance(ex, dict):
        out["exchange"] = {x: ex.get(x) for x in ("subject", "hex") if ex.get(x) is not None}
    return out


def alert_in(a) -> dict:
    """What a source handed AirportAlerts (the decision's inputs, not the stored row)."""
    out = {"source": a.source, "kind": a.kind, "subject": a.subject, "group": a.group, "title": a.title,
           "confidence": a.confidence, "rank": a.rank, "push": a.push, "limit": a.limit,
           "aircraft": [[x.get("callsign"), x.get("hex"), x.get("match")] for x in a.aircraft or []
                        if isinstance(x, dict)]}
    if a.reopen_s:
        out["reopen_s"] = a.reopen_s
    return out


# -- running fixtures -------------------------------------------------------------------------------------------------


def load(name: str) -> list[dict]:
    path = FIXTURES / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip() and not line.startswith("//")]


def names() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.jsonl"))


def run_scenario(steps: list[dict], tmp: Path) -> dict:
    head = steps[0] if steps and steps[0].get("do") == "scenario" else {}
    body = steps[1:] if head else steps
    t0 = NIGHT if head.get("t0") == "night" else DAY
    with Rig(tmp, t0, head.get("levels", (2, 3)), head.get("atc_enabled", True)) as rig:
        return {"why": head.get("why", ""), "steps": [rig.step(s) for s in body]}


def run_rules(cases: list[dict], tmp: Path) -> dict:
    out = []
    with Rig(tmp, DAY) as rig:
        for n, case in enumerate(cases):
            rig.base, rig.ids = DAY + n * CASE_GAP_S, Ids()     # each case's times from its own start, its own names
            got = [rig.step(s) for s in case["steps"]]
            out.append({"rule": case["rule"], "expect": case["expect"], "why": case.get("why", ""), "steps": got})
            rig.at(CASE_GAP_S - 60)                         # the next case starts clean: this one's pre-alerts expired
            rig.app.cad.airport_alerts.tick()
            rig.app.cad.airport_watch.tick([], rig.clock())
            rig._baseline()
    return {"cases": out}


def run(name: str, tmp: Path) -> dict:
    steps = load(name)
    return run_rules(steps, tmp) if name == "rules" else run_scenario(steps, tmp)


def wiring(tmp: Path) -> dict:
    with Rig(tmp) as rig:
        return rig.wiring()


def golden_path(name: str) -> Path:
    return FIXTURES / f"{name}.golden.json"


def dump(d: dict) -> str:
    return json.dumps(d, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def diff(name: str, got: dict) -> str:
    path = golden_path(name)
    want = path.read_text() if path.exists() else ""
    have = dump(got)
    if want == have:
        return ""
    return "".join(difflib.unified_diff(want.splitlines(True), have.splitlines(True), f"{path.name} (committed)",
                                        f"{path.name} (now)", n=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Regenerate (--write) or compare the airport pre-alert goldens.")
    ap.add_argument("names", nargs="*", help="fixtures (default: all, and the wiring)")
    ap.add_argument("--write", action="store_true", help="write the goldens (read the diff before committing them)")
    a = ap.parse_args(argv)
    todo = a.names or [*names(), "wiring"]
    bad = 0
    for name in todo:
        with tempfile.TemporaryDirectory() as tmp:
            got = wiring(Path(tmp)) if name == "wiring" else run(name, Path(tmp))
        d = diff(name, got)
        if d and a.write:
            golden_path(name).write_text(dump(got))
            print(f"{name}: written", file=sys.stderr)
        elif d:
            bad += 1
            sys.stdout.write(d)
        else:
            print(f"{name}: unchanged", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
