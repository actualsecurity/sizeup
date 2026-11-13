"""The intel replay / backtest (sizeup/intel/replay.py, tools/intel_replay.py) and its county-CAD auto-labels
(sizeup/intel/labels.py), radio intel plan PR 7. The real intel engine (sizeup/intel/engine.py) over synthetic rows, and a
small stand-in with the engine's interface where a test needs exact, placed events (the engine places only through the
gazetteer). The private `mrrs-intel` replay test runs the real engine over captured sets kept outside git.

Synthetic data only: Example Ave / Sample St, made-up units, invented points, invented wording."""

import gzip
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from sizeup.cad.sources import FEED_SOURCE
from sizeup.intel import labels as lab
from sizeup.intel import replay as rp
from tests.test_cad import make_cad

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import intel_replay  # noqa: E402
import replay_sync  # noqa: E402

T0 = 1_800_000_000.0
PLACES = {"100 example ave": (41.0000, -73.6600), "200 sample st": (41.0100, -73.6700)}
FORBIDDEN = ("incidents", "radio_links", "incoming", "alarms")


class Toy:
    """A stand-in with the engine's replay interface: a phrase per category, an event per row that hits, placed by a
    fixed lookup. It writes nothing anywhere."""

    def __init__(self, sink, rules, clock, *_):
        self.sink, self.clock = sink, clock
        self.rules = json.loads(Path(rules).read_text()) if rules else {"fire_report": "smoke", "dispute": "argument"}
        self.n = 0

    def feed(self, row):
        text = (row.get("text") or "").lower()
        for cat, phrase in self.rules.items():
            if phrase not in text:
                continue
            self.n += 1
            rid = f"pd.{cat}"
            self.sink.hit({"traffic_id": row["id"], "rule_id": rid, "rule_rev": 1, "weight": 0.7, "category": cat})
            where = next((k for k in PLACES if k in text), None)
            lat, lon = PLACES[where] if where else (None, None)
            ev = {"id": f"e{self.n}", "category": cat, "first_at": self.clock(), "traffic_ids": [row["id"]],
                  "rules": [[rid, 1, 0.7]], "lat": lat, "lon": lon, "precision": "address" if where else "none",
                  "subject": f"{cat}:{where}"}
            self.sink.event(ev, "raised")
            self.sink.push(ev, {"mode": "push", "would_push": cat == "fire_report", "suppressed": "",
                                "message": {"title": "invented", "body": "invented"}})

    def tick(self, now):
        pass

    def ruleset_hash(self):
        return "toy1"


def toy(sink, rules, clock, *_):
    return Toy(sink, rules, clock)


def row(cad, tid, start, text, service="police", system="mrrs", tg=9211, status="done", model="ggml-large-v3-turbo.bin"):
    with cad.store.tx() as db:
        db.execute("INSERT INTO radio_traffic (id, start, end, channel, talkgroup, text, model, status, source, system, "
                   "service, updated_at) VALUES (?, ?, ?, 'PD Dispatch', ?, ?, ?, ?, ?, ?, ?, ?)",
                   (tid, start, start + 5, tg, text, model, status, f"p25:{system}:{tg}", system, service, start))


def incident(cad, typ, lat, lon, created, subtype="", source=FEED_SOURCE, ref="", status="closed"):
    with cad.store.tx() as db:
        return db.execute("INSERT INTO incidents (type, subtype, status, source, source_ref, created_at, lat, lon, address) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '100 Example Ave')",
                          (typ, subtype, status, source, ref, created, lat, lon)).lastrowid


def report(cad, iid, ref, first_seen, source="cadfeed", merged="", dispatched=None):
    data = {"type": "INVENTED-CODE", "location": "100 EXAMPLE AVE", "times": {"dispatched": dispatched} if dispatched else {}}
    with cad.store.tx() as db:
        db.execute("INSERT INTO source_reports (source, ref, rank, incident_id, first_seen, updated_at, data, merged_into) "
                   "VALUES (?, ?, 1, ?, ?, ?, ?, ?)", (source, ref, iid, first_seen, first_seen, json.dumps(data), merged))


def county(cad, ref, first_seen, typ, lat, lon, subtype="", **kw):
    """A county call: the feed's incident and its report."""
    iid = incident(cad, typ, lat, lon, first_seen, subtype, ref=f"feed:cadfeed:{ref}")
    report(cad, iid, ref, first_seen, **kw)
    return iid


@pytest.fixture
def restored(tmp_path):
    """A synthetic cad.sqlite shaped like a local restore: police rows, the county feed's reports, a paging one."""
    cad = make_cad(tmp_path)
    row(cad, "r1", T0, "unit 12 smoke showing 100 example ave")                  # tp: 600 s before the county
    row(cad, "r2", T0 + 3600, "unit 14 smoke from 200 sample st")                # ~950 m from the county's: false place
    row(cad, "r3", T0 + 7200, "verbal argument in the lot")                      # unlabelled class
    row(cad, "r4", T0 + 9000, "smoke in the area")                               # no point: time_only
    row(cad, "r5", T0 + 9100, "", status="skipped", model="ggml (skipped: busy 610 s)")   # a shed clip
    row(cad, "f1", T0 + 100, "engine 60 responding", service="fire", system="westchester", tg=307)
    a1 = county(cad, "A1", T0 + 600, "STRU", 41.0001, -73.6601)
    report(cad, a1, "A1-2", T0 + 800)                                            # a second report, same incident: one label
    county(cad, "A2", T0 + 3700, "STRU", 41.0040, -73.6620)
    county(cad, "A3", T0 + 9050, "INVEST", None, None)
    page = incident(cad, "STRU", 41.0, -73.66, T0 + 500, source="PAGE")
    report(cad, page, "p1", T0 + 500, source="paging")                           # our own decoder: never a label
    report(cad, a1, "A5", T0 + 500, merged="A1")                                 # merged away: not a report
    county(cad, "A6", T0 + 20000, "ALS", 41.02, -73.65)                          # outside the rows' span
    gone = county(cad, "A7", T0 + 4000, "STRU", 41.0, -73.66)
    with cad.store.tx() as db:
        db.execute("UPDATE incidents SET status = 'dismissed' WHERE id = ?", (gone,))   # dismissed: never a label
    county(cad, "A8", T0 + 5000, "ALARM", 41.0, -73.66, subtype="COMM")          # a plain fire alarm: unlabelled
    county(cad, "A9", T0 + 5100, "ALARM", 41.0, -73.66, subtype="CO-RESD")       # CO: hazmat
    cad.store.db.close()
    return tmp_path / "cad.sqlite"


def counts(path):
    con = rp.open_ro(path)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in FORBIDDEN}
    finally:
        con.close()


# -- the real engine -----------------------------------------------------------------------------------------------------

def test_the_real_engine_replays_read_only_and_writes_nothing(restored):
    before, rows_before = restored.read_bytes(), counts(restored)
    con = rp.open_ro(restored)
    rows = rp.load_rows(con)
    settings = rp.read_settings(con)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO incoming (ref, at, kind, text, until) VALUES ('x', 0, 'radio', '', 0)")
    run = rp.replay(rows, settings=settings)
    con.close()
    m = rp.metrics(run)
    assert len(rows) == 6 and m["events"] >= 3 and run.ruleset            # the shipped police pack's hash
    cats = {e["category"] for e in run.rec.events.values()}
    assert cats == {"fire_report"}
    assert m["pushes"] >= 1 and m["rules"]["pd.smoke_seen"]["hits"] >= 2
    for ds in run.rec.decisions.values():                                   # the composed push is never kept
        assert all("message" not in d for d in ds)
    assert any(d["has_message"] for ds in run.rec.decisions.values() for d in ds)
    assert restored.read_bytes() == before and counts(restored) == rows_before
    assert not list(restored.parent.glob("*.sqlite-wal")) or before == restored.read_bytes()


def test_the_clock_is_each_rows_start(restored):
    con = rp.open_ro(restored)
    rows = rp.load_rows(con)
    con.close()
    run = rp.replay(rows)
    firsts = sorted(e["first_at"] for e in run.rec.events.values())
    raised = sorted(e["raised_at"] for e in run.rec.events.values())
    assert firsts[0] == T0 and raised[0] == T0                               # r1's start, not its end


def test_settings_from_the_copy_turn_the_engine_off(restored, tmp_path):
    cad = make_cad(tmp_path)                                                 # the same file, written as the hub would
    with cad.store.tx() as db:
        db.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES ('intel', 'enabled', 'false', 0)")
    cad.store.db.close()
    con = rp.open_ro(restored)
    rows, settings = rp.load_rows(con), rp.read_settings(con)
    con.close()
    assert settings["intel"] == {"enabled": False}
    assert rp.replay(rows, settings=settings).rec.events == {}


def test_recorder_clear_and_digest():
    rec = rp.Recorder(lambda: 0.0)
    rec.hit({"traffic_id": "t1", "rule_id": "a"})
    rec.hit({"traffic_id": "t1", "rule_id": "b"})
    rec.hit({"traffic_id": "t2", "rule_id": "a"})
    rec.clear("t1", {"b"})
    assert [(h["traffic_id"], h["rule_id"]) for h in rec.hits] == [("t1", "b"), ("t2", "a")]
    rec.digest([object(), object()])
    assert (rec.digests, rec.digested) == (1, 2)


def test_no_writable_connection_anywhere_in_the_tools():
    """Every SQLite connection the replay, the labels and the evaluation open is read-only (mode=ro): a restored copy
    (or anything else) is never written."""
    import re
    root = Path(__file__).resolve().parent.parent
    for rel in ("sizeup/intel/replay.py", "sizeup/intel/labels.py", "tools/intel_replay.py", "tools/stt_eval.py",
                "sizeup/cad/sttfix.py"):
        src = (root / rel).read_text()
        for m in re.finditer(r"sqlite3\.connect\(([^)]*)\)", src):
            assert "mode=ro" in m.group(1) and "uri=True" in m.group(1), (rel, m.group(0))


# -- the labels: a pure read -----------------------------------------------------------------------------------------------

def test_county_labels_are_a_pure_read_of_stored_incidents(restored):
    con = rp.open_ro(restored)
    assert lab.county_feeds(con) == ["cadfeed"]
    incs = lab.county_incidents(con, T0, T0 + 20000)
    con.close()
    # A1 (two reports: one), A2, A3, A9 (CO alarm: hazmat), A6; never the paging report, the merged one, the dismissed
    # incident or the plain fire alarm
    assert [x["cls"] for x in incs] == ["fire", "fire", "hazmat", "fire", "aided"]
    assert incs[0]["t"] == T0 + 600                                              # the incident's earliest report
    assert set(incs[0]) == {"t", "cls", "lat", "lon"}                            # never the county's type or location text
    assert "INVENTED" not in json.dumps(incs) and "EXAMPLE" not in json.dumps(incs)
    for q in (lab.QUERY, lab.FEEDS):
        assert q.lstrip().upper().startswith("SELECT")
        for word in ("INSERT", "UPDATE", "DELETE", "REPLACE", "ATTACH", "PRAGMA"):
            assert word not in q.upper()
    assert "source != 'paging'" not in lab.QUERY and "r.source IN" in lab.QUERY   # a positive filter
    src = Path(lab.__file__).read_text()
    for net in ("urllib", "requests", "http.client", "socket", "poke_feed", "sqlite3.connect"):
        assert net not in src                                                      # no request, no connection of its own


def test_only_the_named_feeds_count(restored):
    con = rp.open_ro(restored)
    assert lab.county_incidents(con, T0, T0 + 20000, feeds=["otherfeed"]) == []
    assert lab.county_incidents(con, T0, T0 + 20000, feeds=["paging"]) == []
    assert len(lab.county_incidents(con, T0, T0 + 20000, feeds=["cadfeed"])) == 5
    con.close()


def test_dispatch_time_wins_over_first_seen(tmp_path):
    cad = make_cad(tmp_path)
    county(cad, "B1", T0 + 900, "MVA", 41.0, -73.66, dispatched=T0 + 300)
    assert lab.county_incidents(cad.store.db, T0, T0 + 1000)[0]["t"] == T0 + 300


@pytest.mark.parametrize("typ, sub, cls", [
    ("STRU", "", "fire"), ("STRU", "RESD", "fire"), ("ALARM", "COMM", None), ("ALARM", "", None),
    ("ALARM", "GAS", "hazmat"), ("ALARM", "CO-COMM", "hazmat"), ("HAZARD", "GAS-LEAK", "hazmat"),
    ("HAZARD", "WIRES", None), ("HAZARD", "INVENTED ODOR", "hazmat"), ("MA", "FIRE", "fire"), ("MA", "EMS", "aided"),
    ("MA", "STANDBY", None), ("MVA", "", "mva"), ("ALS", "", "aided"), ("AIRPORT", "ALERT-2", None),
    ("AIRPORT", "ACFT-FIRE", "fire"), ("SERVICE", "", None),
])
def test_class_by_type_and_subtype(typ, sub, cls):
    assert lab.class_of_type(typ, sub) == cls


def test_match_labels():
    incs = [{"t": T0 + 600, "cls": "fire", "lat": 41.0, "lon": -73.66}, {"t": T0, "cls": "mva", "lat": None, "lon": None}]
    ev = lambda cat, lat=None, lon=None, prec="address", t=T0: {"id": cat + str(lat) + prec, "category": cat,  # noqa: E731
                                                              "first_at": t, "lat": lat, "lon": lon, "precision": prec}
    got = [x["label"] for x in lab.match([
        ev("fire_report", 41.0005, -73.66),                        # ~55 m: tp
        ev("smoke_seen", 41.0020, -73.66),                         # ~220 m, address precision: near
        ev("fire_report", 41.0020, -73.66, "block"),               # ~220 m, block precision: tp
        ev("fire", 41.0060, -73.66),                               # ~670 m: false place
        ev("fire", 41.0300, -73.66),                               # 3.3 km: another call
        ev("fire", 41.0, -73.66, t=T0 + 3600),                     # too late
        ev("mva_injury"),                                          # no point, one candidate: time only
        ev("pursuit"),                                             # no class
    ], incs)]
    assert got == ["tp", "near", "tp", "false_place", "none", "none", "time_only", "unlabelled"]
    first = lab.match([ev("fire_report", 41.0005, -73.66)], incs)[0]
    assert first["lead_s"] == 600 and 50 < first["error_m"] < 60
    assert lab.class_of_category("mva_injury") == "mva" and lab.class_of_category("weapons") is None
    assert lab.class_of_category("weapons", {"weapons": "aided"}) == "aided"


# -- metrics over exact events (the stand-in) ---------------------------------------------------------------------------

def test_metrics(restored):
    con = rp.open_ro(restored)
    rows = rp.load_rows(con)
    incs = lab.county_incidents(con, rows[0]["start"], rows[-1]["end"])
    con.close()
    m = rp.metrics(rp.replay(rows, toy), incs)
    assert m["rows"] == 6 and m["events"] == 4 and m["pushes"] == 3 and m["shed"] == 1
    fr = m["categories"]["fire_report"]
    assert fr["events"] == 3 and fr["labelled"] == 3 and fr["right"] == 2 and fr["tp"] == 1 and fr["false_place"] == 1
    assert fr["precision"] == round(2 / 3, 3)
    assert m["categories"]["dispute"]["labelled"] == 0 and m["categories"]["dispute"]["precision"] is None
    assert m["rules"]["pd.fire_report"]["hits"] == 3 and m["rules"]["pd.fire_report"]["precision"] == round(2 / 3, 3)
    r = m["recall"]
    assert r["county_incidents"] == 4 and r["matched"] == 2                  # A1, A2, A3, A9 in the span
    assert r["by_class"] == {"fire": {"n": 3, "matched": 2}, "hazmat": {"n": 1, "matched": 0}}
    assert r["lead_s_median"] == 50 and r["lead_s_p90"] == 600              # the clock: each row's start
    assert m["place"]["address_resolved"] == 1.0
    assert m["place"]["false_place"] == 0.5 and m["place"]["error_m_p90"] > 300
    assert m["latency"]["raise_s_median"] == 0 and m["latency"]["feed_ms_p50"] is not None
    assert rp.metrics(rp.replay(rows, toy))["recall"] is None                # no labels asked for


def test_diff_two_rule_sets(restored, tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"fire_report": "smoke"}))
    b.write_text(json.dumps({"fire_report": "smoke showing", "dispute": "argument"}))
    con = rp.open_ro(restored)
    rows = rp.load_rows(con)
    con.close()
    ra, rb = rp.replay(rows, toy, a), rp.replay(rows, toy, b)
    d = rp.diff(ra, rb, rp.metrics(ra), rp.metrics(rb))
    assert (d["events_gained"], d["events_lost"], d["pushes_gained"], d["pushes_lost"]) == (1, 2, 0, 2)
    assert d["a"]["events"] == 3 and d["b"]["events"] == 2


# -- the tool ---------------------------------------------------------------------------------------------------------------

def test_the_tool_prints_aggregates_only(restored, tmp_path, capsys):
    out = tmp_path / "x.intel-replay.json"
    before = restored.read_bytes()
    assert intel_replay.main(["--db", str(restored), "--labels", "--no-place", "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "county incidents" in printed and "shed clips 1" in printed and "digests" in printed
    for w in ("example", "sample", "smoke showing", "r1", "unit 12", "cadfeed", "invented"):   # rule ids are public
        assert w not in printed.lower()
    assert json.loads(out.read_text())["events"] >= 3
    assert "invented" not in out.read_text().lower()                          # nor in the JSON (no push message)
    assert restored.read_bytes() == before


def test_the_tool_with_the_stand_in(restored, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rp, "core_pipeline", toy)
    assert intel_replay.main(["--db", str(restored), "--labels", "--no-place"]) == 0
    assert "recall 50.0% (2/4 county incidents)" in capsys.readouterr().out
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"fire_report": "smoke"}))
    b.write_text(json.dumps({"fire_report": "smoke showing"}))
    assert intel_replay.main(["--db", str(restored), "--no-place", "--diff", str(a), str(b), "--since", str(T0 + 1)]) == 0
    assert "events +0 -2" in capsys.readouterr().out


def test_capture_a_local_only_set(restored, tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(cache))
    monkeypatch.setattr(rp, "core_pipeline", toy)
    assert intel_replay.main(["--db", str(restored), "--services", "police", "--capture", "test"]) == 0
    from tests.replay import find_sets
    (s,) = find_sets("mrrs-intel")
    assert len(list(s.jsonl("rows.jsonl.gz"))) == 5 and len(list(s.jsonl("incidents.jsonl.gz"))) == 4
    with gzip.open(s.file("incidents.jsonl.gz"), "rt") as f:
        assert all(set(json.loads(x)) == {"t", "cls", "lat", "lon"} for x in f)
    assert json.loads(s.file("settings.json").read_text()) == {"intel": {}, "intel_packs": {}, "intel_rules": {}}
    assert replay_sync.check(cache) == []
    captured = (s.manifest["captured_from"], s.manifest["captured_to"], s.manifest["expires"])
    assert intel_replay.main(["--set", f"mrrs-intel/{s.path.name}", "--labels", "--no-place", "--pin"]) == 0
    exp = json.loads(s.file("expected.json").read_text())
    assert exp["events"] == 4 and exp["right"] == {"fire_report": 2, "dispute": 0}
    (s,) = find_sets("mrrs-intel")
    assert (s.manifest["captured_from"], s.manifest["captured_to"], s.manifest["expires"]) == captured   # kept
    assert replay_sync.check(cache) == []                                          # the manifest written again
    ran = []
    monkeypatch.setattr(replay_sync, "aws", lambda *a: ran.append(a) or 0)
    monkeypatch.setattr(replay_sync, "owned", lambda url: None)
    capsys.readouterr()
    assert replay_sync.main(["push", "--write-to-s3"]) == 0
    assert ran == [] and "kept local: mrrs-intel/" in capsys.readouterr().out       # never pushed
    assert not intel_replay.expired(s.manifest) and intel_replay.expired(s.manifest, s.manifest["expires"] + 1)
    assert intel_replay.purge(30.0) == 0                                          # captured in 2027: not old yet
    assert intel_replay.purge(30.0, now=T0 + 31 * 86400) == 1 and find_sets("mrrs-intel") == []
    assert intel_replay.main(["--purge"]) == 0


# -- the private regression replay: the real engine over captured sets (outside git) --------------------------------------

@pytest.mark.replay("mrrs-intel")
def test_mrrs_intel_regressions(replay_sets):
    """Each captured set (tools/intel_replay.py --capture, --pin) through the real engine: no fewer right, no more
    pushes, no lower recall than pinned (a little slack for ties)."""
    for s in replay_sets("mrrs-intel"):
        exp_p = s.file("expected.json")
        if not exp_p.is_file() or intel_replay.expired(s.manifest):
            continue
        exp = json.loads(exp_p.read_text())
        st = s.file("settings.json")
        settings = json.loads(st.read_text()) if st.is_file() else {}
        rows = list(s.jsonl("rows.jsonl.gz"))
        m = rp.metrics(rp.replay(rows, settings=settings, locate=rp.gazetteer_locator()), list(s.jsonl("incidents.jsonl.gz")))
        assert m["pushes"] <= exp["pushes"] * 1.2 + 1, s.name
        if exp.get("recall") is not None:
            assert (m["recall"] or {}).get("recall", 0) >= exp["recall"] - 0.05, s.name
        for cat, p in (exp.get("precision") or {}).items():
            got = m["categories"].get(cat, {}).get("precision")
            if p is not None and got is not None:
                assert got >= p - 0.05, (s.name, cat)
        for cat, n in (exp.get("right") or {}).items():             # no fewer correct events
            assert m["categories"].get(cat, {}).get("right", 0) >= n, (s.name, cat)


@pytest.mark.parametrize("category, cls", [
    ("fire_report", "fire"), ("smoke_seen", "fire"), ("fireworks", None), ("gas_leak", "hazmat"), ("gasoline_theft", None),
    ("ems_call", "aided"), ("problems", None), ("mva_injury", "mva"), ("aided", "aided"), ("noise complaint", None),
])
def test_category_classes_by_whole_words(category, cls):
    assert lab.class_of_category(category) == cls


def test_ambiguous_and_no_time_labels():
    """Several ungeocoded candidates: ambiguous, not none; an event with no first_at (the engine's Event default 0.0):
    never matched. Neither is in precision's denominator."""
    from sizeup.intel.model import Event
    incs = [{"t": T0 + 60, "cls": "fire", "lat": None, "lon": None}, {"t": T0 + 120, "cls": "fire", "lat": None, "lon": None}]
    got = lab.match([{"id": 1, "category": "fire_report", "first_at": T0},
                     {"id": 2, "category": "fire_report", "first_at": T0, "lat": 41.0, "lon": -73.66},
                     {**rp._d(Event(category="fire_report")), "id": 3},
                     {"id": 4, "category": "fire_report"}], incs)
    assert [x["label"] for x in got] == ["ambiguous", "ambiguous", "no_time", "no_time"]
    assert Event().first_at == 0.0                                                # pinned: the engine's default
    assert "ambiguous" not in rp.LABELLED and "no_time" not in rp.LABELLED and "unlabelled" not in rp.LABELLED


def test_tool_json_never_inside_the_repo(restored):
    with pytest.raises(SystemExit):
        intel_replay.main(["--db", str(restored), "--no-place", "--json", str(intel_replay.ROOT / "x.intel-replay.json")])
    assert not (intel_replay.ROOT / "x.intel-replay.json").exists()
