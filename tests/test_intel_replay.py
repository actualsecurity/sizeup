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
    assert sorted(p.name for p in restored.parent.iterdir()) == ["cad.sqlite"]     # no -wal, no -shm: nothing made


def test_a_copy_with_a_wal_is_read_through_it_and_never_written(tmp_path):
    """A copy taken while its writer was open (a -wal with changes beside it): the rows in the WAL are read, and the
    main file stays the same bytes (never checkpointed by the replay's connection)."""
    cad = make_cad(tmp_path)
    row(cad, "w1", T0, "unit 12 smoke showing 100 example ave")
    path = tmp_path / "cad.sqlite"
    assert Path(f"{path}-wal").stat().st_size > 0
    before = path.read_bytes()
    con = rp.open_ro(path)
    assert [r["id"] for r in rp.load_rows(con)] == ["w1"]
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM radio_traffic")
    con.close()
    assert path.read_bytes() == before
    cad.store.db.close()


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
    assert set(incs[0]) == {"t", "cls", "lat", "lon", "st"}                      # never the county's type or location text
    assert incs[0]["st"] == lab.street_id("7 EXAMPLE AVENUE, Sampletown") and len(incs[0]["st"]) == 12   # a hash only
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
    # ... and once transcribed: the raising row's end (start + 5) + the clip's length (5) + the median STT latency (0:
    # these rows' updated_at is their start, so none is stored)
    assert r["lead_heard_s_median"] == 40 and r["lead_heard_s_p90"] == 590
    assert r["heard_delay"] == {"mode": "clip_length+median_stt_latency", "stt_latency_s_median": None}
    fixed = rp.metrics(rp.replay(rows, toy), incs, stt_delay_s=20)["recall"]
    assert fixed["heard_delay"] == {"mode": "fixed", "seconds": 20.0}
    assert fixed["lead_heard_s_median"] == 25 and fixed["lead_s_median"] == 50      # end + 20 s; the optimistic one as before
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
        assert all(set(json.loads(x)) == {"t", "cls", "lat", "lon", "st"} for x in f)
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


def net_guard(monkeypatch) -> list[tuple]:
    """Records every reach for the network (socket connect/send/bind/listen, create_connection, getaddrinfo, urlopen)
    made by this test's own thread or by a thread it starts (or one those start, and so on), and answers it with
    nothing: recorded, not raised (the engine's own `except Exception` would swallow a raise). A thread already
    running before (pytest-xdist runs many test files in one process: an earlier test's hub answering a station's
    25 s long poll late, #665) is not this test's: its calls go through to the real socket, unrecorded. (So work the
    replay handed to a pool thread started before it would not be seen: the replay starts no pool.)"""
    import socket
    import threading
    import urllib.request
    net: list[tuple] = []
    ours = {threading.current_thread()}
    start = threading.Thread.start

    def started(self, *a, **kw):
        if threading.current_thread() in ours:
            ours.add(self)
        return start(self, *a, **kw)

    def spy(owner, name, answer=None):
        real = getattr(owner, name)
        label = f"socket.{name}" if owner is socket.socket else name

        def call(*a, **kw):
            if threading.current_thread() not in ours:
                return real(*a, **kw)
            net.append((label, a[1:] if owner is socket.socket else a))
            return answer
        monkeypatch.setattr(owner, name, call)
    monkeypatch.setattr(threading.Thread, "start", started)
    for name in ("connect", "connect_ex", "send", "sendall", "sendto", "bind", "listen"):
        spy(socket.socket, name, 0)
    spy(socket, "create_connection")
    spy(socket, "getaddrinfo", [])
    spy(urllib.request, "urlopen")
    return net


def test_the_network_guard_counts_the_replays_own_reaches_and_no_other_threads(restored, monkeypatch):
    """net_guard catches a send from the replay's own thread and from threads the replay starts (and theirs), and
    lets a thread that was already running (an earlier test's, #665) send for real, unrecorded."""
    import socket
    import threading
    import urllib.request
    a, b = socket.socketpair()
    go, done = threading.Event(), threading.Event()

    def leftover():
        go.wait(10)
        a.sendall(b'{"jobs":[]}')                        # what the leftover hub request thread sent in #665
        done.set()
    threading.Thread(target=leftover, daemon=True).start()
    net = net_guard(monkeypatch)
    go.set()
    assert done.wait(10) and b.recv(64) == b'{"jobs":[]}'      # through for real
    a.close()
    b.close()

    class Leaky(Toy):
        def feed(self, row):
            super().feed(row)
            if row["id"] != "r1":
                return
            socket.create_connection(("192.0.2.1", 443))                 # the replay's own thread (TEST-NET-1)
            s = socket.socket()

            def child():
                s.sendall(b"from a thread the replay started")
                grandchild = threading.Thread(target=lambda: urllib.request.urlopen("http://192.0.2.1/"))
                grandchild.start()
                grandchild.join()
            t = threading.Thread(target=child)
            t.start()
            t.join()
            s.close()
    con = rp.open_ro(restored)
    rp.replay(rp.load_rows(con), factory=lambda sink, rules, clock: Leaky(sink, rules, clock))
    con.close()
    assert [x[0] for x in net] == ["create_connection", "socket.sendall", "urlopen"]


def test_a_replay_sends_nothing_and_opens_nothing_writable(restored, monkeypatch):
    """The real engine and the gazetteer over a read-only copy: no socket call is ever made from the replay's thread
    or one it starts (net_guard), the engine has no notifier, no listen links and no model behind it, and every
    SQLite connection is read-only (or in memory). The file is the same bytes afterwards, with nothing beside it."""
    opened, real = [], sqlite3.connect

    def connect(*a, **kw):
        opened.append((str(a[0]), kw.get("uri", False)))
        return real(*a, **kw)
    monkeypatch.setattr(sqlite3, "connect", connect)
    net = net_guard(monkeypatch)
    before, beside = restored.read_bytes(), sorted(p.name for p in restored.parent.iterdir())
    assert intel_replay.main(["--db", str(restored), "--labels"]) == 0
    con = rp.open_ro(restored)
    run = rp.replay(rp.load_rows(con), settings=rp.read_settings(con), locate=rp.gazetteer_locator())
    con.close()
    assert run.rec.events and any(run.rec.would_push(e) for e in run.rec.events)       # it would have pushed: recorded only
    assert net == []                                                                 # nothing reached for the network
    eng = run.engine
    assert type(eng).__name__ == "IntelEngine" and eng.sink is run.rec
    assert eng.infer is None and eng.notifier is None and eng.listen is None and eng.store is None
    assert eng.threaded is False and eng._thread is None and eng.errors == 0
    # nothing that could write a file: mode=ro / immutable URIs, or an index built in memory (the gazetteer's own)
    assert opened and not [(p, uri) for p, uri in opened
                           if p != ":memory:" and not (uri and p.startswith("file:") and ("mode=ro" in p or "immutable=1" in p))]
    assert restored.read_bytes() == before and sorted(p.name for p in restored.parent.iterdir()) == beside


# -- open_ro: one place, never the live database, immutable only for a bare file ------------------------------------------

def test_immutable_only_when_neither_a_wal_nor_a_shm_is_there(restored, monkeypatch):
    uris, real = [], sqlite3.connect

    def connect(*a, **kw):
        uris.append(str(a[0]))
        return real(*a, **kw)
    monkeypatch.setattr(sqlite3, "connect", connect)
    rp.open_ro(restored).close()
    assert uris[-1].endswith("?mode=ro&immutable=1")
    for side in ("-shm", "-wal"):                       # either one there (even empty): through the WAL, never immutable
        p = Path(f"{restored}{side}")
        p.write_bytes(b"")
        rp.open_ro(restored).close()
        assert uris[-1].endswith("?mode=ro"), side
        for q in restored.parent.glob("cad.sqlite-*"):
            q.unlink()
    assert "sqlite3.connect" not in (Path(intel_replay.__file__).parent / "stt_eval.py").read_text()     # one helper
    assert "sqlite3.connect" not in Path(intel_replay.__file__).read_text()


def test_the_live_database_is_refused(restored, tmp_path, monkeypatch, capsys):
    """A path in a running sizeup's state directory ($SIZEUP_STATE_DIR, its state/, the image's /data, a station's
    /var/lib/sizeup, this checkout's state/): refused by open_ro, and by both tools with a clear error (exit 2)."""
    import stt_eval
    assert rp.open_ro(restored) is not None                                   # a copy elsewhere: fine
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path))
    with pytest.raises(rp.LiveDatabase, match="restored copy"):
        rp.open_ro(restored)
    with pytest.raises(rp.LiveDatabase):
        rp.open_ro(tmp_path / "." / "cad.sqlite")                             # however it is spelled
    assert intel_replay.main(["--db", str(restored), "--no-place"]) == 2
    assert "restored copy" in capsys.readouterr().err
    assert stt_eval.main(["--db", str(restored)]) == 2
    assert "restored copy" in capsys.readouterr().err
    monkeypatch.setenv("SIZEUP_STATE_DIR", str(tmp_path.parent))              # <state dir>/state/cad.sqlite too
    (tmp_path.parent / "state").mkdir(exist_ok=True)
    live = tmp_path.parent / "state" / "cad.sqlite"
    live.write_bytes(restored.read_bytes())
    with pytest.raises(rp.LiveDatabase):
        rp.open_ro(live)
    monkeypatch.delenv("SIZEUP_STATE_DIR")
    root = Path(rp.__file__).resolve().parent.parent.parent
    for known in ("/data/cad.sqlite", "/var/lib/sizeup/state/cad.sqlite", root / "state" / "cad.sqlite"):
        with pytest.raises(rp.LiveDatabase):
            rp.refuse_live(known)
    link = tmp_path / "elsewhere.sqlite"
    link.symlink_to(root / "state" / "cad.sqlite")                            # nor through a link to it
    with pytest.raises(rp.LiveDatabase):
        rp.refuse_live(link)


# -- lead time both ways; the same street, the wrong end ------------------------------------------------------------------

def test_stt_latency_from_what_the_rows_store():
    rows = [{"id": "a", "start": 0.0, "end": 4.0, "updated_at": 10.0, "text": "made up"},           # 6 s
            {"id": "b", "start": 10.0, "end": 12.0, "updated_at": 20.0, "text": "made up"},         # 8 s
            {"id": "c", "start": 20.0, "end": 25.0, "updated_at": 35.0, "text": "made up"},         # 10 s
            {"id": "d", "start": 30.0, "end": 31.0, "updated_at": 90000.0, "text": "made up"},      # an edit days on
            {"id": "e", "start": 40.0, "end": 41.0, "updated_at": 43.0, "text": ""},                # no transcript
            {"id": "f", "start": 50.0, "end": 51.0, "updated_at": "", "text": "made up"}]           # not stored
    assert rp.stt_latency(rows) == 8.0 and rp.stt_latency(rows[3:]) is None and rp.stt_latency([]) is None
    assert rp.heard_at(rows[0], 8.0) == 4.0 + 4.0 + 8.0              # end + the clip's length + the median latency
    assert rp.heard_at(rows[0], None) == 8.0 and rp.heard_at(rows[0], 8.0, fixed=30) == 34.0


def test_the_tool_labels_both_lead_times(restored, capsys):
    db = sqlite3.connect(restored)                                  # transcripts that arrived 7 s after each row's end
    db.execute("UPDATE radio_traffic SET updated_at = end + 7 WHERE text != ''")
    db.commit()
    db.close()                                                      # (checkpointed: a bare file again)
    monkeypatch_toy = rp.core_pipeline
    rp.core_pipeline = toy
    try:
        assert intel_replay.main(["--db", str(restored), "--labels", "--no-place"]) == 0
        out = capsys.readouterr().out
        assert "lead from the row's start (optimistic: heard the instant it began): median 50 s · p90 600 s" in out
        assert "lead once transcribed (row end + clip length + median STT latency 7 s): median 33 s · p90 583 s" in out
        assert intel_replay.main(["--db", str(restored), "--labels", "--no-place", "--stt-delay", "60"]) == 0
        assert "lead once transcribed (row end + 60 s): median -15 s · p90 535 s" in capsys.readouterr().out
    finally:
        rp.core_pipeline = monkeypatch_toy


def test_the_same_street_past_two_km_is_a_false_place():
    """An event of the right class and time, more than NEAR_M from the county's point: "none" (no such call), unless
    its place is on the incident's own street: then it is the call, placed at the wrong end (false_place)."""
    far = (41.0300, -73.6600)                                        # ~3.3 km north of the incident
    incs = [{"t": T0 + 300, "cls": "fire", "lat": 41.0000, "lon": -73.6600, "st": lab.street_id("100 Example Ave")}]
    ev = {"id": 1, "category": "fire_report", "first_at": T0, "lat": far[0], "lon": far[1], "precision": "address"}
    same = lab.match([dict(ev, label_full="2400 Example Avenue, Sampletown")], incs)[0]
    assert same["label"] == "false_place" and same["incident"] == 0 and same["error_m"] > lab.NEAR_M and same["lead_s"] == 300
    assert lab.match([dict(ev, label_full="2400 Sample St")], incs)[0]["label"] == "none"          # another street
    assert lab.match([dict(ev, label_full="")], incs)[0]["label"] == "none"                        # no street to compare
    assert lab.match([dict(ev, label_full="2400 Example Ave")], [dict(incs[0], st="")])[0]["label"] == "none"
    assert lab.match([dict(ev, label_full="2400 Example Ave")], [dict(incs[0], t=T0 + 4 * 3600)])[0]["label"] == "none"   # not in time
    near = lab.match([dict(ev, lat=41.0001, lon=-73.6601, label_full="100 Example Ave")], incs)[0]
    assert near["label"] == "tp"                                                                    # within NEAR_M: as before
    # it counts against the place, never as a match for recall
    class R:
        pass
    run = R()
    run.rows = [{"id": "r1", "start": T0, "end": T0 + 5, "status": "done", "model": "m"}]
    run.rec = rp.Recorder(lambda: T0)
    run.rec.feeding = "r1"
    run.rec.event(dict(ev, label_full="2400 Example Avenue", traffic_ids=["r1"], rules=[["pd.x", 1, 0.7]]), "raised")
    run.feed_s, run.ruleset = [0.001], "x"
    m = rp.metrics(run, [dict(incs[0], t=T0 + 3)])
    assert m["categories"]["fire_report"]["false_place"] == 1 and m["place"]["false_place"] == 1.0
    assert m["recall"]["matched"] == 0


@pytest.mark.parametrize("a, b, same", [
    ("100 Example Ave", "2400 EXAMPLE AVENUE, Sampletown", True),
    ("the 400 block of Example Avenue", "12 Example Ave", True),
    ("100 Example Ave", "100 Sample St", False),
    ("Example Ave & Sample St", "9 Example Avenue", True),
    ("", "100 Example Ave", False), ("12", "100 Example Ave", False),
])
def test_street_id(a, b, same):
    assert (lab.street_id(a) == lab.street_id(b) and lab.street_id(a) != "") is same
    assert "example" not in lab.street_id(a).lower()


def test_rows_from_before_the_service_column_are_scoped_as_live_rows_are(tmp_path):
    """A county police talkgroup's row stored with no service (before the radio log carried one) is read with the
    talkgroups file's service, so a replay over the history reaches the packs that read it live."""
    cad = make_cad(tmp_path)
    row(cad, "old-1", 1000.0, "order of gas outside", service="", system="westchester", tg=383)
    row(cad, "old-2", 1010.0, "may day may day", service="", system="westchester", tg=105)
    row(cad, "new-1", 1020.0, "shots fired", service="police", system="mrrs", tg=9211)
    row(cad, "odd-1", 1030.0, "copy", service="", system="westchester", tg=99999)      # not in any file: left as it is
    path = tmp_path / "cad.sqlite"
    cad.store.db.commit()
    con = rp.open_ro(path)
    try:
        rows = {r["id"]: r for r in rp.load_rows(con)}
        police = {r["id"] for r in rp.load_rows(con, services=["police"])}
    finally:
        con.close()
    assert rows["old-1"]["service"] == "police" and rows["old-2"]["service"] == "fire" and rows["odd-1"]["service"] == ""
    assert rows["new-1"]["service"] == "police" and police == {"old-1", "new-1"}
    groups = rp.talkgroup_groups()
    assert groups[("westchester", 383)] == ("County Police", "police") and groups[("mrrs", 9570)][1] == "ems"
    run = rp.replay(sorted(rows.values(), key=lambda r: r["start"]))
    assert {(e["pack"], e["category"]) for e in run.rec.events.values()} >= {("county_police", "hazmat"), ("fire", "mayday")}


def test_an_admins_service_choice_in_the_copy_scopes_rows_stored_without_a_service(tmp_path):
    """#689's per-source service choice, read from the copy's own Settings: a row stored with no service on a talkgroup
    whose file says transit is scoped as the admins chose (fire); the county's keyed as the hub keys it (tg:<n> is
    p25:westchester:<n>); a row's stored service is kept; the process's own choices are back after."""
    from sizeup import sourcekeys
    cad = make_cad(tmp_path)
    cad.comms.context_broadcast = True                 # as on the hub behind Access: every reader is signed in (#419)
    cad.settings_post("radio_services", {"operator": "Lt. Test", "action": "set_many",
                                         "services": {"p25:mrrs:9121": "fire", "tg:383": "ems"}})
    row(cad, "old-1", 1000.0, "smoke showing", service="", system="mrrs", tg=9121)          # its file: transit
    row(cad, "kept-1", 1010.0, "copy", service="transit", system="mrrs", tg=9121)           # stored: never changed
    row(cad, "old-2", 1020.0, "copy", service="", system="westchester", tg=383)             # its file: police
    cad.store.db.commit()
    assert rp.talkgroup_groups()[("mrrs", 9121)][1] == "fire"          # (the copy's cad set the process's here)
    sourcekeys.set_service_overrides({"p25:mrrs:9122": "police"}, hub=True)      # the process's own: a station's
    con = rp.open_ro(tmp_path / "cad.sqlite")
    try:
        assert rp.read_service_overrides(con) == {"p25:mrrs:9121": "fire", "p25:westchester:383": "ems"}
        rows = {r["id"]: r for r in rp.load_rows(con)}
        fire = {r["id"] for r in rp.load_rows(con, services=["fire"])}
    finally:
        con.close()
    assert rows["old-1"]["service"] == "fire" and rows["old-2"]["service"] == "ems"
    assert rows["kept-1"]["service"] == "transit" and fire == {"old-1"}
    assert sourcekeys.service_overrides() == {"p25:mrrs:9122": "police"} and sourcekeys._service_from_hub
    cad.store.db.close()


def test_without_a_choice_in_the_copy_the_file_says_and_the_process_choices_stay_out(tmp_path):
    from sizeup import sourcekeys
    cad = make_cad(tmp_path)
    row(cad, "old-1", 1000.0, "smoke showing", service="", system="mrrs", tg=9121)
    cad.store.db.commit()
    sourcekeys.set_service_overrides({"p25:mrrs:9121": "fire"})        # this process's: not the copy's
    con = rp.open_ro(tmp_path / "cad.sqlite")
    try:
        assert rp.read_service_overrides(con) == {}
        (r,) = rp.load_rows(con)
    finally:
        con.close()
    assert r["service"] == "transit"
    assert sourcekeys.service_overrides() == {"p25:mrrs:9121": "fire"}
    cad.store.db.close()


def test_service_choices_for_a_block_are_put_back_even_when_it_fails():
    from sizeup import sourcekeys
    sourcekeys.set_service_overrides({"fm:FG1": "other"}, hub=True)
    with pytest.raises(RuntimeError):
        with sourcekeys.service_overrides_as({"p25:mrrs:9121": "fire", "tg:383": "nonsense"}):
            assert sourcekeys.service_overrides() == {"p25:mrrs:9121": "fire"}
            assert sourcekeys.service("MNR Fire Brigade", "transit", key="p25:mrrs:9121") == "fire"
            raise RuntimeError("boom")
    assert sourcekeys.service_overrides() == {"fm:FG1": "other"} and sourcekeys._service_from_hub
    assert sourcekeys.service("MNR Fire Brigade", "transit", key="p25:mrrs:9121") == "transit"
