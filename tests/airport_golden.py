"""Golden characterisation of today's airport pre-alerts (the pipeline migration's PR 1): tests only, no code changed.

Invented airband and fire-radio scenarios (tests/fixtures/pipeline/airband/*.jsonl) run through the real entry points of
a real App (App._transcribed / App._retext -> App._atc -> AtcWatch, AtcTalkers, AirportWatch; Cad._on_text ->
AirportWatch.heard_radio; App._atc_view -> AirportWatch.tick; the Aircraft layer's emergency glue; the console's
accept / dismiss / state) with:

    an injected clock       the Cad's (and so its AirportAlerts, AirportWatch, AtcTalkers, Sources, Store), the AtcWatch's
                            and sizeup.app's own time.time() (the Aircraft picture's freshness): every time in a golden
                            is seconds from the scenario's t0, and nothing depends on the wall clock
    a fake Aircraft layer   what App._atc_aircraft reads (view, wide, st); its picture is fetched at the scenario's time
    ntfy at the wire        sizeup.notify.urlopen replaced: everything in sizeup.notify runs as in production (the topic
                            in effect for the kind, publish(): the static backstop, the outbound guard, the server and
                            topic checks, compose(title, who), priority, truncation, tags, click, actions) and the JSON
                            body that would be POSTed is recorded, with the KIND it went to named by the topic's role
                            ("alerts", "status", "intel"; the topics are invented, never a real one's name). The outbound
                            guard is armed as an all-in-one start arms it (off); the invented ntfy server is this
                            machine's address, which the guard always allows
    a recording poll-now    Cad.sources.trigger (the station's sources_poll_now): the county feed's poke reasons

Two goldens per scenario:

    <name>.golden.json      OUTCOMES, strict: after every step the airport_alerts rows that changed (read from the table,
                            without the lazy expiry a console read would run), new or changed incidents, new CAD
                            event-log lines, what was sent to ntfy, the poll-now reasons, the picture widening and what
                            the console answered. The migration must keep these byte for byte.
    <name>.trace.json       INTERNALS: the decisions behind them (spies around AtcWatch.heard / Rules.hits /
                            AtcWatch.confidence, AtcTalkers.heard, AirportWatch.heard_atc / heard_radio, AirportAlerts.
                            raise_alert / add_to) and the watches' log lines. Its own test; the migration may regenerate
                            it on purpose (--write-trace) as the internals move. A spy whose attribute is gone records
                            "unavailable" instead of failing.

Row and incident ids are named by order of creation in the scenario (A1, A2 ...; I1 ...).

Steps (one JSON object per line; "t" is seconds from the scenario's t0, and the clock is set to it first):

    {"do": "scenario", "t0": "day" | "night", "levels": [2, 3], "atc_enabled": true, "why": "..."}   first line, optional
    {"do": "picture", "t": 0, "planes": [{...}], "wide": [{...}]}   the Aircraft layer's picture (App._atc_view)
    {"do": "air", "t": 10, "id": "twr-1", "ch": "HPN Tower", "dur": 4, "ago": 0, "text": "..."}
                                    a new airband transmission ending `ago` s before t (App._transcribed)
    {"do": "retext", "t": 20, "id": "twr-1", "text": "..."}        new text for one (App._retext, a re-run)
    {"do": "radio", "t": 30, "id": "fire-1", "tg": 307, "ch": "Fire/EMS 15", "text": "..."}   a P25 transmission
    {"do": "adsb", "t": 25, "what": "start" | "update" | "again" | "end", "ev": {...}, "why": "..."}
                                    an aircraft emergency from the Aircraft layer's watch (Rig._adsb)
    {"do": "accept" | "dismiss", "t": 40, "alert": "A1", "operator": "...", "reason": "..."}   the console's answer
    {"do": "read", "t": 50}         the console reads the CAD state (the lazy expiry runs here)
    {"do": "wait", "t": 60}         the clock moves, nothing else

rules.jsonl holds one case per line ({"rule", "expect": "fires" | "quiet", "words" (fires: the rule's words, as stored),
"via" (quiet: how it stays quiet), "why", "steps"}), all run in one App, each CASE_GAP_S after the last with the
previous case's pre-alerts expired first, so no case sees another's.

Regenerate deliberately (never on a test run), then read the diff before committing it:

    .venv/bin/python -m tests.airport_golden --write              # every scenario, outcomes and traces
    .venv/bin/python -m tests.airport_golden --write-trace        # the traces only (internals moved, outcomes not)
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
import time as real_time
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
NTFY = "http://127.0.0.1:9"       # this machine (the outbound guard's always-allowed addresses); urlopen never connects
FIRE_TG = 307                     # a fire talkgroup 60 Control is heard on
OUTCOMES = ("console", "alerts", "incidents", "cad_log", "pushes", "push_failures", "county_feed_poll_now",
            "aircraft_widened_nm")
ROW_COLS = ("id", "source", "kind", "subject", "grp", "at", "updated_at", "title", "detail", "confidence", "channel",
            "data", "count", "status", "outcome_at", "incident_id", "operator", "note")
INCIDENT_COLS = ("number", "type", "subtype", "status", "source", "source_ref", "emergency", "received_via", "address",
                 "lat", "lon", "geo_label", "remarks", "acft_type", "acft_nature", "acft_runway", "created_at",
                 "accepted_at")


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


class SceneTime:
    """sizeup.app's `time` module during a scenario: time() is the scenario's clock, the rest the real module's."""

    def __init__(self, clock: Clock):
        self.time = clock

    def __getattr__(self, name):
        return getattr(real_time, name)


class FakeLayer:
    """The Aircraft layer as App._atc_aircraft and App._atc read it (st, view, wide, _lock, widen)."""

    def __init__(self):
        self.view: list[dict] = []
        self.wide: list[dict] = []
        self._lock = threading.Lock()
        self.st: dict = {}
        self.widened: list[float] = []

    def widen(self, nm: float) -> None:
        self.widened.append(nm)


class Sent:
    """What urlopen returns: ntfy's answer."""

    def __init__(self, n: int):
        self.body = json.dumps({"id": f"push-{n}"}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1) -> bytes:
        return self.body


class Ids:
    """Rows and incidents named by order of creation: A1, A2 ...; I1 ..."""

    def __init__(self):
        self.a: dict[int, str] = {}
        self.i: dict[int, str] = {}

    def alert(self, pid) -> str | None:
        if pid is None:
            return None
        return self.a.setdefault(int(pid), f"A{sum(1 for v in self.a.values() if v.startswith('A')) + 1}")

    def incident(self, iid) -> str | None:
        if iid is None:
            return None
        return self.i.setdefault(int(iid), f"I{len(self.i) + 1}")

    def text(self, s: str) -> str:
        """Ids inside text (log lines, a source_ref): '#12', 'alert-level:12', 'event 7' by their names."""
        s = re.sub(r"#(\d+)\b", lambda m: "#" + (self.a.get(int(m.group(1))) or m.group(1)), s)
        s = re.sub(r"alert-level:(\d+)", lambda m: "alert-level:" + (self.a.get(int(m.group(1))) or m.group(1)), s)
        return re.sub(r"\bevent (\d+)\b", lambda m: "event " + (self.i.get(int(m.group(1))) or m.group(1)), s)


def _get(x, *path, default="unavailable"):
    """x.a.b ..., or "unavailable" when an internal attribute is gone (a spy never fails on one)."""
    for p in path:
        x = x.get(p, default) if isinstance(x, dict) else getattr(x, p, default)
        if x is default:
            return default
    return x


class Rig:
    """One App wired as production wires it, with the fakes above. Use as a context manager."""

    def __init__(self, tmp: Path, t0: float = DAY, levels=(2, 3), atc_enabled: bool = True):
        from sizeup import app as app_mod
        from sizeup.app import App
        from sizeup.cad.service import Cad
        from tests.appcfg import cfg
        self.base = t0
        self.clock = Clock(t0)
        self.ids = Ids()
        self.trace: list[dict] = []
        self.unavailable: list[str] = []
        self.pushes: list[dict] = []
        self.pokes: list[str] = []
        self.logs: list[str] = []
        self.console: dict | None = None
        self._patches = [mock.patch("sizeup.notify.urlopen", self._urlopen),
                         mock.patch.object(app_mod, "time", SceneTime(self.clock))]
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
        from sizeup import outbound
        for p in self._patches:
            p.start()
        outbound.arm("all-in-one", env={})            # as `python -m sizeup` all-in-one: off but for this machine
        assert outbound.check("ntfy", NTFY) == "" and outbound.check("ntfy", "https://ntfy.sh") != ""
        self._baseline()
        return self

    def __exit__(self, *exc) -> None:
        from sizeup import outbound
        self._flush()
        outbound.disarm()
        for p in reversed(self._patches):
            p.stop()
        try:
            self.app.cad.shutdown()
        except Exception:  # noqa: BLE001 - a test's App: nothing to keep
            pass

    # -- the fakes and spies ----------------------------------------------------------------------------------------

    def _urlopen(self, req, timeout=None):
        """sizeup.notify.publish's request: the JSON body it would POST to the configured server."""
        assert req.full_url == NTFY + "/" and req.get_method() == "POST", req.full_url
        msg = json.loads(req.data)
        topic = msg.pop("topic")
        role = next((k for k, v in TOPICS.items() if v == topic), "unknown topic")
        self.pushes.append({"to": role, **msg})
        return Sent(len(self.pushes))

    def _wrap(self, obj, name: str, label: str, make) -> None:
        fn = getattr(obj, name, None)
        if not callable(fn):
            self.unavailable.append(label)
            return
        setattr(obj, name, make(fn))

    def _spy(self) -> None:
        app, rig = self.app, self
        watch, aw, aa, talkers = app.atc, app.cad.airport_watch, app.cad.airport_alerts, app.cad.atc_talkers
        if watch is not None:
            self._spy_atc(watch)

        def k_heard(fn):
            def heard(t):
                rec = fn(t)
                rig.trace.append({"watch": "talkers", "id": t.get("id"), "record": talker(rec)})
                return rec
            return heard
        self._wrap(talkers, "heard", "AtcTalkers.heard", k_heard)

        def aw_atc(fn):
            def heard_atc(t, rec=None):
                row = fn(t, rec)
                rig.trace.append({"watch": "airport_watch", "id": t.get("id"), "row": _pid(row)})
                return row
            return heard_atc

        def aw_radio(fn):
            def heard_radio(t):
                row = fn(t)
                if t.get("channel") not in (_get(aw, "airband", default=()) or ()):   # (_on_text: every row)
                    rig.trace.append({"watch": "60 Control", "id": t.get("id"), "row": _pid(row)})
                return row
            return heard_radio
        self._wrap(aw, "heard_atc", "AirportWatch.heard_atc", aw_atc)
        self._wrap(aw, "heard_radio", "AirportWatch.heard_radio", aw_radio)

        def aa_raise(fn):
            def raise_alert(a):
                row, new = fn(a)
                rig.trace.append({"raise": alert_in(a), "row": _pid(row),
                                  "how": ("new" if new else "added") if row else "dropped"})
                return row, new
            return raise_alert

        def aa_add(fn):
            def add_to(pid, a):
                row = fn(pid, a)
                rig.trace.append({"add_to": int(pid), "alert": alert_in(a), "row": _pid(row)})
                return row
            return add_to
        self._wrap(aa, "raise_alert", "AirportAlerts.raise_alert", aa_raise)
        self._wrap(aa, "add_to", "AirportAlerts.add_to", aa_add)

        for obj, name in ((aa, "airport_alerts"), (watch, "atc"), (aw, "airport_watch")):
            if obj is not None:
                obj.log = (lambda n: lambda m: rig.logs.append(f"{n}: {m}"))(name)

    def _spy_atc(self, watch) -> None:
        """AtcWatch.heard with what its rules hit, the exchange it read and the score (Rules.hits, confidence)."""
        rig, cur = self, {}

        def w_heard(fn):
            def heard(t):
                cur.clear()
                entry = {"watch": "atc", "id": t.get("id"), "ch": t.get("source"), "fed": t.get("text"), "hits": None}
                rig.trace.append(entry)
                row = fn(t)
                entry.update({k: v for k, v in cur.items() if k in ("hits", "context", "score", "adsb")})
                entry["row"] = _pid(row)
                return row
            return heard

        def r_hits(fn):
            def hits(norm):
                out = fn(norm)
                cur["hits"] = [[_get(h, "rule", "id"), _get(h, "text")] for h in out]
                return out
            return hits

        def w_conf(fn):
            def confidence(me, ctx):
                c, adsb = fn(me, ctx)
                cur["score"], cur["adsb"] = round(c, 6), list(adsb) if adsb else None
                cur["context"] = [_get(x, "id") for x in ctx]
                return c, adsb
            return confidence
        self._wrap(watch, "heard", "AtcWatch.heard", w_heard)
        rules = getattr(watch, "rules", None)
        if rules is None:
            self.unavailable.append("AtcWatch.rules")
        else:
            self._wrap(rules, "hits", "Rules.hits", r_hits)
        self._wrap(watch, "confidence", "AtcWatch.confidence", w_conf)

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
        """The table as stored (no lazy expiry, no reader's decoration)."""
        rows = self.app.cad.store.q(f"SELECT {', '.join(ROW_COLS)} FROM airport_alerts ORDER BY id")
        return {r["id"]: r for r in rows}

    def _incidents(self) -> dict[int, dict]:
        return {r["id"]: r for r in self.app.cad.store.q(f"SELECT id, {', '.join(INCIDENT_COLS)} FROM incidents "
                                                          "ORDER BY id")}

    def _failed(self) -> dict:
        return {id(n): (n.failed, n.last_error) for n in self.app.pushes.made()}

    def _baseline(self) -> None:
        self._flush()
        self.rows, self.incs, self.fails = self._rows(), self._incidents(), self._failed()
        self.last_event = (self.app.cad.store.one("SELECT MAX(id) AS n FROM events") or {}).get("n") or 0
        self.trace.clear()
        self.pushes.clear()
        self.pokes.clear()
        self.logs.clear()
        self.layer.widened.clear()
        self.console = None

    def _flush(self) -> None:
        for n in self.app.pushes.made():
            n.flush(10.0)

    def row(self, r: dict) -> dict:
        ids = self.ids
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            data = r["data"]
        return {"id": ids.alert(r["id"]), "source": r["source"], "kind": r["kind"], "subject": r["subject"],
                "group": r["grp"], "at": self.rel(r["at"]), "updated_at": self.rel(r["updated_at"]), "title": r["title"],
                "detail": r["detail"], "confidence": r["confidence"], "channel": r["channel"], "count": r["count"],
                "status": r["status"], "outcome_at": self.rel(r["outcome_at"]), "incident": ids.incident(r["incident_id"]),
                "operator": r["operator"], "note": r["note"], "data": self.rel(data)}

    def incident(self, r: dict) -> dict:
        out = {"id": self.ids.incident(r["id"])}
        for k in INCIDENT_COLS:
            v = r[k]
            out[k] = self.ids.text(v) if k == "source_ref" and v else self.rel(v)
        return out

    def _names(self, x):
        """The trace's raw row ids ("row", "add_to") by their names."""
        if isinstance(x, list):
            return [self._names(v) for v in x]
        if isinstance(x, dict):
            return {k: (self.ids.alert(v) if k in ("row", "add_to") and isinstance(v, int) else self._names(v))
                    for k, v in x.items()}
        return x

    def observe(self, step: dict) -> tuple[dict, dict]:
        """(the step's outcomes, its internals): the module doc."""
        self._flush()
        rows, incs, fails = self._rows(), self._incidents(), self._failed()
        for pid in sorted(rows):                          # names by creation, whatever the spies saw first
            self.ids.alert(pid)
        for iid in sorted(incs):
            self.ids.incident(iid)
        out: dict = {"step": step}
        if self.console is not None:
            out["console"] = self.console
        changed = [self.row(r) for pid, r in sorted(rows.items()) if self.rows.get(pid) != r]
        if changed:
            out["alerts"] = changed
        new_incs = [self.incident(r) for iid, r in sorted(incs.items()) if self.incs.get(iid) != r]
        if new_incs:
            out["incidents"] = new_incs
        events = self.app.cad.store.q("SELECT id, incident_id, at, kind, text, operator FROM events WHERE id > ? "
                                      "ORDER BY id", (self.last_event,))
        if events:
            out["cad_log"] = [{"incident": self.ids.incident(e["incident_id"]), "at": self.rel(e["at"]),
                               "kind": e["kind"], "text": self.ids.text(e["text"]), "operator": e["operator"]}
                              for e in events]
        if self.pushes:
            out["pushes"] = list(self.pushes)
        failed = [err for k, (n, err) in fails.items() if n > self.fails.get(k, (0, ""))[0]]
        if failed:
            out["push_failures"] = failed
        if self.pokes:
            out["county_feed_poll_now"] = list(self.pokes)
        if self.layer.widened:
            out["aircraft_widened_nm"] = list(self.layer.widened)
        inner: dict = {"step": {k: step[k] for k in ("do", "t", "id") if k in step}}
        if self.trace:
            inner["decisions"] = self.rel(self._names(self.trace))
        if self.logs:
            inner["log"] = [self.ids.text(x) for x in self.logs]
        self._baseline()
        return out, inner

    # -- steps ------------------------------------------------------------------------------------------------------

    def at(self, t: float) -> float:
        self.clock.t = self.base + float(t)
        return self.clock.t

    def step(self, s: dict) -> tuple[dict, dict]:
        from sizeup.incidents import Traffic
        app = self.app
        do = s["do"]
        now = self.at(float(s.get("t", 0)))
        if do == "picture":
            self.layer.view = [dict(p) for p in s.get("planes") or []]
            self.layer.wide = [dict(p) for p in s.get("wide") or []]
            self.layer.st = {"at": now, "fetched": now}       # App._atc_aircraft: fresh for 60 s of the scenario's clock
            app._atc_view(list(self.layer.view), now)
        elif do in ("air", "radio"):
            end = now - float(s.get("ago", 0))
            src = s.get("ch", "HPN Tower" if do == "air" else "Fire/EMS 15")
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
                self.console = {do: s["alert"], "status": r["status"], "acknowledged": bool(r.get("acknowledged")),
                                "incident": self.ids.incident((r.get("incident") or {}).get("id"))}
            except Exception as e:  # noqa: BLE001 - what the console would show
                self.console = {do: s["alert"], "error": f"{type(e).__name__}: {e}"}
        elif do == "read":
            st = app.cad.state()["airport_alerts"]
            self.console = {"read": "open", "open": [self.ids.alert(r["id"]) for r in st["open"]]}
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
# the airport's settings that Rig builds from the Config dataclass's defaults (tests.appcfg); config.load() must give
# the same for a config file that sets none of them (test_the_config_file_defaults_are_what_the_goldens_ran_on)
CONFIG_FIELDS = ("atc_enabled", "atc_rules", "atc_context", "atc_context_s", "atc_dedupe_min", "atc_expire_min",
                 "atc_max_per_hour", "atc_alert_level_incident", "atc_airport_names")
MINIMAL_CONFIG = '[radio]\ntones_file = ""\n[[radio.channels]]\nlabel = "HPN Tower"\nmhz = 118.575\nkind = "airband"\n'


def _plain(v):
    return sorted(v) if isinstance(v, (set, frozenset)) else list(v) if isinstance(v, tuple) else v


def config_file_defaults(tmp: Path) -> dict:
    """What config.load() gives the airport's settings for a config file that sets none of them."""
    import os
    from sizeup.config import load
    p = tmp / "minimal.toml"
    p.write_text(MINIMAL_CONFIG)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SIZEUP_")}     # the file's, not this shell's
    with mock.patch.dict(os.environ, env, clear=True):
        c = load(p)
    return {k: _plain(getattr(c, k)) for k in CONFIG_FIELDS}


def dataclass_defaults() -> dict:
    """The Config dataclass's own defaults for the same settings (what tests.appcfg.cfg() gives Rig)."""
    import dataclasses
    from sizeup.config import Config
    out = {}
    for f in dataclasses.fields(Config):
        if f.name in CONFIG_FIELDS:
            v = f.default if f.default is not dataclasses.MISSING else f.default_factory()   # type: ignore[misc]
            out[f.name] = _plain(v)
    return {k: out[k] for k in CONFIG_FIELDS}


def _pid(row) -> int | None:
    return row.get("id") if isinstance(row, dict) else None


def talker(rec: dict | None) -> dict | None:
    """The talker record as the airport watch reads it (aircraft, exchange) and who was talking."""
    if not isinstance(rec, dict):
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
    out = {k: _get(a, k) for k in ("source", "kind", "subject", "group", "title", "confidence", "rank", "push", "limit")}
    acft = _get(a, "aircraft", default=[])
    out["aircraft"] = [[x.get("callsign"), x.get("hex"), x.get("match")] for x in acft or [] if isinstance(x, dict)]
    if _get(a, "reopen_s", default=0):
        out["reopen_s"] = a.reopen_s
    return out


# -- running fixtures -------------------------------------------------------------------------------------------------


def load(name: str) -> list[dict]:
    path = FIXTURES / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip() and not line.startswith("//")]


def names() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.jsonl"))


def run_scenario(steps: list[dict], tmp: Path) -> tuple[dict, dict]:
    head = steps[0] if steps and steps[0].get("do") == "scenario" else {}
    body = steps[1:] if head else steps
    t0 = NIGHT if head.get("t0") == "night" else DAY
    with Rig(tmp, t0, head.get("levels", (2, 3)), head.get("atc_enabled", True)) as rig:
        got = [rig.step(s) for s in body]
        return ({"why": head.get("why", ""), "steps": [o for o, _ in got]},
                {"unavailable": rig.unavailable, "steps": [i for _, i in got]})


def run_rules(cases: list[dict], tmp: Path) -> tuple[dict, dict]:
    out, inner = [], []
    with Rig(tmp, DAY) as rig:
        for n, case in enumerate(cases):
            rig.base, rig.ids = DAY + n * CASE_GAP_S, Ids()     # each case's times from its own start, its own names
            for pid in rig.rows:                                # (the earlier cases' rows: not this case's A1 ...)
                rig.ids.a[pid] = f"earlier-{pid}"
            for iid in rig.incs:
                rig.ids.i[iid] = f"earlier-{iid}"
            got = [rig.step(s) for s in case["steps"]]
            head = {k: case[k] for k in ("rule", "expect", "words", "via", "why") if k in case}
            out.append({**head, "steps": [o for o, _ in got]})
            inner.append({"rule": case["rule"], "expect": case["expect"], "steps": [i for _, i in got]})
            rig.at(CASE_GAP_S - 60)                         # the next case starts clean: this one's pre-alerts expired
            rig.app.cad.airport_alerts.tick()
            rig.app.cad.airport_watch.tick([], rig.clock())
            rig._baseline()
        return {"cases": out}, {"unavailable": rig.unavailable, "cases": inner}


def run(name: str, tmp: Path) -> tuple[dict, dict]:
    """(outcomes, internals) of a fixture."""
    steps = load(name)
    return run_rules(steps, tmp) if name == "rules" else run_scenario(steps, tmp)


def wiring(tmp: Path) -> dict:
    with Rig(tmp) as rig:
        return {**rig.wiring(), "config_file_defaults": config_file_defaults(tmp)}


def golden_path(name: str) -> Path:
    return FIXTURES / f"{name}.golden.json"


def trace_path(name: str) -> Path:
    return FIXTURES / f"{name}.trace.json"


def dump(d: dict) -> str:
    return json.dumps(d, indent=1, ensure_ascii=False, sort_keys=False) + "\n"


def diff(path: Path, got: dict) -> str:
    want = path.read_text() if path.exists() else ""
    have = dump(got)
    if want == have:
        return ""
    return "".join(difflib.unified_diff(want.splitlines(True), have.splitlines(True), f"{path.name} (committed)",
                                        f"{path.name} (now)", n=2))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Regenerate (--write, --write-trace) or compare the airport pre-alert goldens.")
    ap.add_argument("names", nargs="*", help="fixtures (default: all, and the wiring)")
    ap.add_argument("--write", action="store_true", help="write outcomes and traces (read the diff before committing)")
    ap.add_argument("--write-trace", action="store_true", help="write the traces only (the internals)")
    a = ap.parse_args(argv)
    todo = a.names or [*names(), "wiring"]
    bad = 0
    for name in todo:
        with tempfile.TemporaryDirectory() as tmp:
            if name == "wiring":
                pairs = [(golden_path(name), wiring(Path(tmp)), a.write)]
            else:
                outcomes, internals = run(name, Path(tmp))
                pairs = [(golden_path(name), outcomes, a.write), (trace_path(name), internals, a.write or a.write_trace)]
        for path, got, write in pairs:
            d = diff(path, got)
            if d and write:
                path.write_text(dump(got))
                print(f"{path.name}: written", file=sys.stderr)
            elif d:
                bad += 1
                sys.stdout.write(d)
            else:
                print(f"{path.name}: unchanged", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
