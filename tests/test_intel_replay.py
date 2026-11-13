"""The intel replay / backtest (sizeup/intel/replay.py, tools/intel_replay.py) and its county-CAD auto-labels
(sizeup/intel/labels.py), radio intel plan PR 7. Driven here by a toy pipeline with the intel core's interface (the core
itself is PR 1); the private `mrrs-intel` replay test runs the real core over captured sets kept outside git.

Synthetic data only: Example Ave / Sample St, made-up units, invented points."""

import gzip
import json
import sqlite3
import sys
from pathlib import Path

import pytest

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
    """A stand-in with the intel core's replay interface: a phrase per category, an event per row that hits, placed
    by a fixed lookup. Raises on a write anywhere: it has none."""

    def __init__(self, sink, rules, clock):
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
            self.sink.push(ev, {"mode": "push", "would_push": cat == "fire_report", "suppressed": ""})

    def tick(self, now):
        pass

    def ruleset_hash(self):
        return "toy1"


def toy(sink, rules, clock):
    return Toy(sink, rules, clock)


def row(cad, tid, start, text, service="police", system="mrrs", tg=9211, status="done", model="ggml-large-v3-turbo.bin"):
    with cad.store.tx() as db:
        db.execute("INSERT INTO radio_traffic (id, start, end, channel, talkgroup, text, model, status, source, system, "
                   "service, updated_at) VALUES (?, ?, ?, 'PD Dispatch', ?, ?, ?, ?, ?, ?, ?, ?)",
                   (tid, start, start + 5, tg, text, model, status, f"p25:{system}:{tg}", system, service, start))


def county(cad, ref, first_seen, typ, lat, lon, source="cadfeed", merged="", dispatched=None):
    with cad.store.tx() as db:
        iid = db.execute("INSERT INTO incidents (type, status, source, created_at, lat, lon, address) VALUES (?, 'closed', 'feed', ?, ?, ?, ?)",
                         (typ, first_seen, lat, lon, "100 Example Ave")).lastrowid
        data = {"type": "COUNTY-ONLY-CODE", "location": "100 EXAMPLE AVE", "times": {"dispatched": dispatched} if dispatched else {}}
        db.execute("INSERT INTO source_reports (source, ref, rank, incident_id, first_seen, updated_at, data, merged_into) "
                   "VALUES (?, ?, 1, ?, ?, ?, ?, ?)", (source, ref, iid, first_seen, first_seen, json.dumps(data), merged))
    return iid


@pytest.fixture
def restored(tmp_path):
    """A synthetic cad.sqlite shaped like a local restore: police rows, county CAD reports, one paging report."""
    cad = make_cad(tmp_path)
    row(cad, "r1", T0, "car 12 smoke showing 100 example ave")                   # tp: 600 s before the county
    row(cad, "r2", T0 + 3600, "car 14 smoke from 200 sample st")                 # ~950 m from the county's: false place
    row(cad, "r3", T0 + 7200, "verbal argument in the lot")                      # unlabelled class
    row(cad, "r4", T0 + 9000, "smoke in the area")                               # no point: time_only
    row(cad, "r5", T0 + 9100, "", status="skipped", model="ggml (skipped: busy 610 s)")   # a shed clip
    row(cad, "f1", T0 + 100, "engine 60 responding", service="fire", system="westchester", tg=307)
    county(cad, "A1", T0 + 600, "STRU", 41.0001, -73.6601)
    county(cad, "A2", T0 + 3700, "STRU", 41.0040, -73.6620)                      # ~650 m from 200 sample st
    county(cad, "A3", T0 + 9050, "INVEST", None, None)
    county(cad, "A4", T0 + 500, "STRU", 41.0, -73.66, source="paging")          # our own decoder: never a label
    county(cad, "A5", T0 + 500, "STRU", 41.0, -73.66, merged="A1")              # merged away: never a label
    county(cad, "A6", T0 + 20000, "ALS", 41.02, -73.65)                          # a class with no event: a miss
    cad.store.db.close()
    return tmp_path / "cad.sqlite"


def counts(path):
    con = sqlite3.connect(path)
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in FORBIDDEN}
    finally:
        con.close()


def test_replay_is_read_only_and_writes_nothing(restored):
    before, rows_before = restored.read_bytes(), counts(restored)
    con = rp.open_ro(restored)
    rows = rp.load_rows(con)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO incoming (ref, at, kind, text, until) VALUES ('x', 0, 'radio', '', 0)")
    run = rp.replay(rows, toy)
    con.close()
    assert len(rows) == 6 and run.rec.events and run.ruleset == "toy1"
    assert restored.read_bytes() == before and counts(restored) == rows_before


def test_county_labels_are_a_pure_read_of_stored_incidents(restored):
    con = rp.open_ro(restored)
    incs = lab.county_incidents(con, T0, T0 + 20000)
    con.close()
    assert [x["cls"] for x in incs] == ["fire", "fire", "fire", "aided"]           # no paging, nothing merged
    assert set(incs[0]) == {"t", "cls", "lat", "lon"}                            # never the county's type or location text
    assert "COUNTY" not in json.dumps(incs) and "EXAMPLE" not in json.dumps(incs)
    assert lab.QUERY.lstrip().upper().startswith("SELECT") and "source != 'paging'" in lab.QUERY
    for word in ("INSERT", "UPDATE", "DELETE", "REPLACE", "ATTACH", "PRAGMA"):
        assert word not in lab.QUERY.upper()
    src = Path(lab.__file__).read_text()
    for net in ("urllib", "requests", "http.client", "socket", "poke_feed"):
        assert net not in src                                                      # no request to anyone


def test_dispatch_time_wins_over_first_seen(tmp_path):
    cad = make_cad(tmp_path)
    county(cad, "B1", T0 + 900, "MVA", 41.0, -73.66, dispatched=T0 + 300)
    con = cad.store.db
    assert lab.county_incidents(con, T0, T0 + 1000)[0]["t"] == T0 + 300


def test_match_labels():
    incs = [{"t": T0 + 600, "cls": "fire", "lat": 41.0, "lon": -73.66}, {"t": T0, "cls": "mva", "lat": None, "lon": None}]
    ev = lambda cat, lat=None, lon=None, prec="address", t=T0: {"id": cat + str(lat), "category": cat, "first_at": t,  # noqa: E731
                                                              "lat": lat, "lon": lon, "precision": prec}
    got = {x["id"]: x for x in lab.match([
        ev("fire_report", 41.0005, -73.66),                        # ~55 m: tp
        ev("smoke_seen", 41.0020, -73.66),                         # ~220 m, address precision: near
        ev("fire_report", 41.0020, -73.66, "block"),               # ~220 m, block precision: tp
        ev("fire", 41.0060, -73.66),                               # ~670 m: false place
        ev("fire", 41.0300, -73.66),                               # 3.3 km: another call
        ev("fire", 41.0, -73.66, t=T0 + 3600),                     # too late
        ev("mva_injury"),                                          # no point, one candidate: time only
        ev("pursuit"),                                             # no class
    ], incs)}
    assert [got[k]["label"] for k in got] == ["tp", "near", "tp", "false_place", "none", "none", "time_only", "unlabelled"]
    assert got["fire_report41.0005"]["lead_s"] == 600 and 50 < got["fire_report41.0005"]["error_m"] < 60
    assert lab.class_of_category("mva_injury") == "mva" and lab.class_of_category("weapons") is None
    assert lab.class_of_category("weapons", {"weapons": "aided"}) == "aided"


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
    assert r["county_incidents"] == 3 and r["matched"] == 2 and r["by_class"] == {"fire": {"n": 3, "matched": 2}}
    assert r["lead_s_median"] == 45 and r["lead_s_p90"] == 595 and m["place"]["address_resolved"] == 1.0   # raised at the end
    assert m["place"]["false_place"] == 0.5 and m["place"]["error_m_p90"] > 300
    assert m["latency"]["raise_s_median"] == 5 and m["latency"]["feed_ms_p50"] is not None
    assert rp.metrics(rp.replay(rows, toy))["recall"] is None                            # no labels asked for


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


def test_the_tool_prints_aggregates_only(restored, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rp, "core_pipeline", toy)
    out = tmp_path / "x.intel-replay.json"
    assert intel_replay.main(["--db", str(restored), "--labels", "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "recall 66.7% (2/3 county incidents)" in printed and "shed clips 1" in printed
    for w in ("example", "sample", "smoke", "r1", "car 12", "cadfeed", "county-only"):
        assert w not in printed.lower()
    assert json.loads(out.read_text())["events"] == 4
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"fire_report": "smoke"}))
    b.write_text(json.dumps({"fire_report": "smoke showing"}))
    assert intel_replay.main(["--db", str(restored), "--diff", str(a), str(b), "--since", str(T0 + 1)]) == 0
    assert "events +0 -2" in capsys.readouterr().out


def test_without_the_core_the_tool_says_so(restored, capsys):
    try:
        import sizeup.intel.engine  # noqa: F401
        pytest.skip("the intel core is here")
    except ImportError:
        pass
    assert intel_replay.main(["--db", str(restored)]) == 2
    assert "PR 1" in capsys.readouterr().err


def test_capture_a_local_only_set(restored, tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    monkeypatch.setenv("SIZEUP_REPLAY_DIR", str(cache))
    monkeypatch.setattr(rp, "core_pipeline", toy)
    assert intel_replay.main(["--db", str(restored), "--services", "police", "--capture", "test"]) == 0
    from tests.replay import find_sets
    (s,) = find_sets("mrrs-intel")
    assert len(list(s.jsonl("rows.jsonl.gz"))) == 5 and len(list(s.jsonl("incidents.jsonl.gz"))) == 3
    with gzip.open(s.file("incidents.jsonl.gz"), "rt") as f:
        assert all(set(json.loads(x)) == {"t", "cls", "lat", "lon"} for x in f)
    assert replay_sync.check(cache) == []
    assert intel_replay.main(["--set", f"mrrs-intel/{s.path.name}", "--labels", "--pin"]) == 0
    assert json.loads(s.file("expected.json").read_text())["events"] == 4
    assert replay_sync.check(cache) == []                                          # the manifest written again
    ran = []
    monkeypatch.setattr(replay_sync, "aws", lambda *a: ran.append(a) or 0)
    monkeypatch.setattr(replay_sync, "owned", lambda url: None)
    capsys.readouterr()
    assert replay_sync.main(["push", "--write-to-s3"]) == 0
    assert ran == [] and "kept local: mrrs-intel/" in capsys.readouterr().out       # never pushed


# -- the private regression replay: the real intel core over captured sets (outside git) -----------------------------

@pytest.mark.replay("mrrs-intel")
def test_mrrs_intel_regressions(replay_sets):
    """Each captured set (tools/intel_replay.py --capture, --pin) through the real core: no fewer right, no more pushes,
    no lower recall than pinned (a little slack for ties)."""
    pytest.importorskip("sizeup.intel.engine", reason="the intel core (PR 1) isn't on this branch yet")
    for s in replay_sets("mrrs-intel"):
        exp_p = s.file("expected.json")
        if not exp_p.is_file():
            continue
        exp = json.loads(exp_p.read_text())
        rows = list(s.jsonl("rows.jsonl.gz"))
        m = rp.metrics(rp.replay(rows), list(s.jsonl("incidents.jsonl.gz")))
        assert m["pushes"] <= exp["pushes"] * 1.2 + 1, s.name
        if exp.get("recall") is not None:
            assert (m["recall"] or {}).get("recall", 0) >= exp["recall"] - 0.05, s.name
        for cat, p in (exp.get("precision") or {}).items():
            got = m["categories"].get(cat, {}).get("precision")
            if p is not None and got is not None:
                assert got >= p - 0.05, (s.name, cat)
