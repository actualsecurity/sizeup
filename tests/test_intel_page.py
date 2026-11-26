"""The Intel page (sizeup/intel/eventlog.py, GET /api/cad/intel/list, static/intel-page.js, /cad#intel).

The admin (2026-11-25): "a page in the app where we can view these intel events even if they do not trigger". Every
event at every level and status, newest first, paged on the hub; why each didn't push (every value the engine and the
policy write to Event.suppressed, and the reasons derived from its mode and status); decided by rules, the model or
both; Useful / Noise / Should have pushed into intel_feedback; anyone signed in, nobody else.

Events come from the engine on the test_intel transcripts where the path is the engine's own, and are written in the
engine's exact shape (model.Event, SqlEvents.save) for each hold the tests need. Invented places only (Sample St &
Test Rd, 12 Example Road)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from sizeup.intel import eventlog
from sizeup.intel.model import Event
from tests.test_cad import make_cad
from tests.test_intel import KEY, FakeNotifier, ctx_row, example_locate
from tests.test_intel import Clock as IntelClock
from tests.test_roles import CASEY, JORDAN, VAL, call, people

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")
T0 = 1_800_000_000.0


def get(api, path, who=CASEY, query=""):
    code, raw, *_ = api.get(path, query, Identity(email=who) if who else None)
    return code, json.loads(raw) if raw[:1] == b"{" else {}


@pytest.fixture
def site(tmp_path):
    clock = IntelClock(T0)
    cad = make_cad(tmp_path, clock=clock)
    people(cad)
    cad.intel.notifier, cad.intel.locate = FakeNotifier(), example_locate
    return cad, clock


def heard(cad, tid, text, at):
    cad.comms.record(ctx_row(tid, text, at))
    cad.intel.consider(tid)
    cad.intel.drain()


def save(cad, **kw) -> Event:
    """An event in the engine's own shape (what SqlEvents.save writes)."""
    base = {"pack": "police", "category": "weapons", "subject": "police:weapons:x", "source_key": KEY, "system": "mrrs",
            "service": "police", "first_at": T0, "last_at": T0, "until": T0 + 3600, "status": "open", "what_score": 0.7,
            "level": "medium", "rules": [["pd.shots", 1, 0.7]], "mode": "log", "updated_at": T0,
            "traffic_ids": ["c1-t0"]}
    base.update(kw)
    return cad.intel.events.save(Event(**base))


def listing(cad, who=VAL, **q):
    code, d = get(CadApi(cad), "/api/cad/intel/list", who, "&".join(f"{k}={v}" for k, v in q.items()))
    assert code == 200, d
    return d


# -- access --------------------------------------------------------------------------------------------------------------

def test_anyone_signed_in_reads_it_and_nobody_else(site):
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    api = CadApi(cad)
    assert get(api, "/api/cad/intel/list", None) == (403, {"error": "radio pre-alerts need a sign-in (Cloudflare Access)"})
    for who in (CASEY, JORDAN, VAL):                    # admin, dispatcher, viewer: not an admin read
        code, d = get(api, "/api/cad/intel/list", who)
        assert code == 200 and [e["category"] for e in d["events"]] == ["weapons"]
    assert "/api/cad/intel/list" not in __import__("sizeup.cad.api", fromlist=["ADMIN_READS"]).ADMIN_READS


def test_the_server_refuses_it_without_access(site):
    import threading
    import urllib.error
    import urllib.request

    from sizeup.web.server import WebServer
    cad, _clock = site

    class Access:
        def identify(self, headers):
            e = headers.get("X-Test-User")
            return Identity(email=e) if e else None
    srv = WebServer("127.0.0.1", 0, {}, lambda: {}, lambda c: None, lambda i: False, cad=CadApi(cad), access=Access(),
                    access_log=lambda m: None, role="hub")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.httpd.server_address[1]}/api/cad/intel/list"
    try:
        def code(hdr):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        assert code({}) == 401
        assert code({"X-Test-User": VAL}) == 200
    finally:
        srv.shutdown()


# -- the row ---------------------------------------------------------------------------------------------------------

def test_a_pushed_event_from_the_engine_is_a_terse_row(site):
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    [e] = listing(cad)["events"]
    assert {k: e[k] for k in ("level", "would", "held", "decided", "place", "precision", "confirmed", "source", "system",
                              "label", "score_level", "n")} == {
        "level": "push", "would": "", "held": None, "decided": "rules", "place": "Sample St & Test Rd, Rye",
        "precision": "intersection", "confirmed": True, "source": "MRRS PD", "system": "mrrs", "label": "Weapon / shots",
        "score_level": "medium", "n": 1}
    assert e["parts"] == [["pd.shots", 0.75]] and e["pushed_at"] == T0


def test_the_source_name_is_the_one_resolvers_with_a_fallback(site):
    cad, _clock = site
    save(cad, source_key="p25:county2:4401", system="county2")       # never heard in the radio log: no name there
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    names = {e["source_key"]: e["source"] for e in listing(cad)["events"]}
    assert names == {KEY: "MRRS PD", "p25:county2:4401": "county2 4401"}
    assert eventlog.source_name({}, "fm:FG1") == "FG1" and eventlog.source_name({}, "tg:307") == "TG 307"
    assert eventlog.source_name({"fm:FG1": "Fireground 1"}, "fm:FG1") == "Fireground 1"


# -- why it didn't push: every value the engine writes ---------------------------------------------------------------------

def written_suppressed() -> set[str]:
    """Every Event.suppressed value the engine, the policy and the highway gate can write, read from their source: a
    new one fails here until the page names it."""
    out = set()
    eng = (ROOT / "sizeup/intel/engine.py").read_text()
    out |= set(re.findall(r'suppressed = "([a-z_]+)"', eng))
    out |= set(re.findall(r'return "(fd_[a-z_]+)"', eng))
    out |= {f"fd_{x}" for x in ("dispatched", "there")}              # f"fd_{ev.fd}" (on_scene returns its own literal)
    out |= set(re.findall(r'why or "([a-z_]+)"', eng))
    out |= set(re.findall(r'why = "([a-z_]+)" if', eng))
    out |= set(re.findall(r'Decision\(d\.mode, False, "([a-z_]+)"', eng))
    pol = (ROOT / "sizeup/intel/policy.py").read_text()
    out |= set(re.findall(r'Decision\(ev\.mode, False, "([a-z_]+)"', pol))
    out |= {"shadow"}                                                # "shadow" if ev.mode == "shadow"
    hw = (ROOT / "sizeup/intel/highway.py").read_text()
    out |= set(re.findall(r'return "(unplaced|out_of_area)"', hw))
    unpl = re.search(r"def _unplaced.*?(?=\n    def )", eng, re.S).group(0)
    out |= set(re.findall(r'"(unplaced|out_of_area)"', unpl))
    model = (ROOT / "sizeup/intel/model.py").read_text()
    doc = re.search(r"suppressed: str = \"\"\s+#([^\n]*\n\s+#[^\n]*)", model).group(1)
    out |= {w for w in re.findall(r"[a-z_]+", doc)}
    out.discard("")
    return out


def test_every_suppressed_value_the_engine_writes_has_a_label():
    found = written_suppressed()
    assert {"unplaced", "out_of_area", "near_incident", "placed_late", "fd_on_scene", "fd_dispatched", "fd_there",
            "fd_late", "rate", "quiet", "off", "shadow", "merged", "scrub", "unsent"} <= found       # the scan works
    assert found - set(eventlog.SUPPRESSED) == set()


@pytest.mark.parametrize("value", sorted(eventlog.SUPPRESSED))
def test_each_suppressed_value_is_its_held_reason_and_its_filter(site, value):
    cad, _clock = site
    held = save(cad, suppressed=value, mode="log" if value in eventlog.ENGINE_HOLDS else "push")
    save(cad, first_at=T0 - 60, pushed_at=T0 - 60, mode="push")       # pushed: no reason
    rows = listing(cad)["events"]
    e = next(r for r in rows if r["id"] == held.id)
    assert e["held"]["key"] == value and e["held"]["label"] == eventlog.SUPPRESSED[value][0]
    assert e["held"]["tip"] == eventlog.SUPPRESSED[value][1]
    assert [r["held"] for r in rows if r["id"] != held.id] == [None]
    assert [r["id"] for r in listing(cad, held=value)["events"]] == [held.id]
    if value in eventlog.POLICY_HOLDS:
        assert e["level"] == "held" and e["would"] == "push"
    if value in eventlog.ENGINE_HOLDS:                # held at log: the rung its rules reach (pd.shots at push)
        assert e["level"] == "log" and e["would"] == "push"


def test_derived_reasons_from_mode_settings_model_and_status(site):
    cad, _clock = site
    ia = cad.intel_admin
    ia.settings_op("pack", {"pack": "neighbor_police", "mode": "log"}, "Casey")
    ia.settings_op("category", {"pack": "police", "category": "edp", "mode": "map"}, "Casey")
    pack = save(cad, pack="neighbor_police", category=next(iter(cad.intel.pack("neighbor_police").categories)),
                rules=[], mode="log")
    cat = save(cad, category="edp", rules=[], mode="map")
    rung = save(cad, mode="map")                                      # pack and category at push: its rules' rung
    unc = save(cad, rules=[["model.pc", 1, 0.92]], mode="map", precision="none")
    unc2 = save(cad, rules=[["model.pc", 1, 0.92]], mode="map", precision="address", label_full="12 Example Road, Rye",
                lat=41.0, lon=-73.7, confirmed=False)
    cap = save(cad, rules=[["model.pc", 1, 0.92]], mode="map", precision="address", label_full="12 Example Road, Rye",
               lat=41.0, lon=-73.7, confirmed=True)
    cue = save(cad, rules=[["model.pc", 1, 0.92], ["pd.dispatch_cue", 1, 0.2]], mode="map")
    sh = save(cad, mode="shadow")
    dis = save(cad, mode="push", status="dismissed")
    exp = save(cad, mode="log", status="expired")           # (its status at every mode: #906 review)
    end = save(cad, mode="map", status="ended")
    pend = save(cad, mode="push")
    rows = {r["id"]: r for r in listing(cad, size=100)["events"]}
    want = {pack.id: "mode_pack", cat.id: "mode_category", rung.id: "mode_rule", unc.id: "uncorroborated",
            unc2.id: "uncorroborated", cap.id: "model_cap", cue.id: "model_cap", sh.id: "shadow", dis.id: "dismissed",
            exp.id: "expired", end.id: "ended", pend.id: "not_pushed"}
    assert {i: rows[i]["held"]["key"] for i in want} == want
    assert rows[pack.id]["held"]["label"] == "pack log" and rows[cat.id]["held"]["label"] == "category map"
    assert rows[rung.id]["held"]["label"] == "rules map"
    assert rows[unc.id]["held"]["detail"] == "no cue · no rule · unplaced"
    assert rows[unc2.id]["held"]["detail"] == "no cue · no rule · unconfirmed place"
    assert rows[unc.id]["would"] == "push" and rows[cap.id]["would"] == "push"
    assert rows[cat.id]["would"] == rows[pack.id]["would"] == "push"         # held by a mode: its rules' rung
    for i, key in want.items():                                      # the filter is the same classification
        assert i in [r["id"] for r in listing(cad, held=key, size=100)["events"]], key
    # Corroborate off: the model's map events are capped, never "uncorroborated"
    ia.settings_op("set", {"key": "infer_pd_corroborate", "value": False}, "Casey")
    assert listing(cad, held="uncorroborated")["events"] == []
    assert {r["id"] for r in listing(cad, held="model_cap", size=100)["events"]} == {unc.id, unc2.id, cap.id, cue.id}


def test_held_and_level_filters_reject_nonsense(site):
    cad, _clock = site
    api = CadApi(cad)
    assert get(api, "/api/cad/intel/list", VAL, "level=loud")[0] == 400
    assert get(api, "/api/cad/intel/list", VAL, "decided=magic")[0] == 400
    assert get(api, "/api/cad/intel/list", VAL, "held=DROP%20TABLE")[0] == 400
    assert get(api, "/api/cad/intel/list", VAL, "from=yesterday")[0] == 400
    for bad in ("nan", "inf", "-inf", "1e999"):
        assert get(api, "/api/cad/intel/list", VAL, f"from={bad}")[0] == 400, bad


# -- decided by --------------------------------------------------------------------------------------------------------

def test_decided_is_stored_and_read_back_from_an_older_releases_rows(site):
    cad, _clock = site
    a = save(cad, rules=[["model.pc", 1, 0.95], ["pd.shots", 1, 0.4]])
    b = save(cad, rules=[["infer.bedrock", 1, 0.8]])
    assert {r["id"]: r["decided"] for r in cad.store.q("SELECT id, decided FROM intel_events")} == {a.id: "both", b.id: "model"}
    with cad.store.tx() as db:                       # an older release's save(): no column value
        db.execute("UPDATE intel_events SET decided = NULL")
    assert {r["id"]: r["decided"] for r in listing(cad)["events"]} == {a.id: "both", b.id: "model"}
    assert [r["id"] for r in listing(cad, decided="both")["events"]] == [a.id]
    from sizeup.intel.events import SqlEvents
    SqlEvents(cad.store)                             # the next start fills them in
    assert {r["id"]: r["decided"] for r in cad.store.q("SELECT id, decided FROM intel_events")} == {a.id: "both", b.id: "model"}


@pytest.mark.parametrize("rules,want", [
    ([["pd.shots", 1, 0.7]], "rules"),
    ([["pd.shots", 1, 0.7], ["pd.training", 1, -0.5]], "rules"),           # a damper is a rule
    ([["model.pc", 1, 0.95]], "model"),
    ([["infer.bedrock", 1, 0.8]], "model"),                                # the LLM stage is the model too
    ([["model.pc", 1, 0.95], ["pd.shots", 1, 0.4]], "both"),
    ([], ""),
])
def test_decided_by_rules_model_or_both_in_python_and_sql(site, rules, want):
    cad, _clock = site
    e = save(cad, rules=rules)
    assert eventlog.decided_by(rules) == want
    [row] = listing(cad)["events"]
    assert row["id"] == e.id and row["decided"] == want
    for d in ("rules", "model", "both"):
        assert [r["id"] for r in listing(cad, decided=d)["events"]] == ([e.id] if d == want else [])


def test_model_parts_are_the_models_and_the_rest_their_weights(site):
    cad, _clock = site
    save(cad, rules=[["model.pc", 1, 0.95], ["pd.shots", 1, 0.4]])
    [row] = listing(cad)["events"]
    assert row["parts"] == [["model.pc", 0.95], ["pd.shots", 0.4]]


# -- filters, range and pages ------------------------------------------------------------------------------------------

def test_filters_level_category_source_system_and_status(site):
    cad, _clock = site
    a = save(cad, pushed_at=T0, mode="push")
    b = save(cad, category="edp", mode="map")
    c = save(cad, source_key="p25:county2:4401", system="county2", mode="log", suppressed="out_of_area")
    d = save(cad, mode="push", suppressed="quiet", status="expired")
    ids = lambda **q: {r["id"] for r in listing(cad, **q)["events"]}       # noqa: E731
    assert ids(level="push") == {a.id} and ids(level="map") == {b.id} and ids(level="log") == {c.id}
    assert ids(level="held") == {d.id} and ids(level="push,held") == {a.id, d.id}
    assert ids(category="edp") == {b.id}
    assert ids(source="p25:county2:4401") == {c.id} and ids(system="county2") == {c.id}
    assert ids(system="mrrs") == {a.id, b.id, d.id}
    assert ids(status="expired") == {d.id}
    f = listing(cad)["facets"]
    assert {s["key"]: s["n"] for s in f["sources"]} == {KEY: 3, "p25:county2:4401": 1}
    assert {c["key"] for c in f["categories"]} == {"weapons", "edp"} and {s["key"] for s in f["systems"]} == {"mrrs", "county2"}


def test_newest_first_paged_on_the_hub_last_24_hours_by_default(site):
    cad, clock = site
    clock.t = T0 + 3600
    made = [save(cad, first_at=T0 - i * 60, last_at=T0 - i * 60).id for i in range(120)]
    old = save(cad, first_at=T0 - 2 * 86400, last_at=T0 - 2 * 86400)
    p0, p1, p2 = (listing(cad, page=p) for p in (0, 1, 2))
    assert p0["total"] == 120 and p0["size"] == eventlog.PAGE_SIZE
    got = [e["id"] for p in (p0, p1, p2) for e in p["events"]]
    assert got == made and len(p2["events"]) == 20                    # newest first, no overlap, no gap
    assert old.id not in got
    assert len(listing(cad, size=1000)["events"]) == eventlog.PAGE_MAX
    day = __import__("datetime").datetime.fromtimestamp(T0 - 2 * 86400, __import__("sizeup.cad.store", fromlist=["TZ"]).TZ)
    rng = listing(cad, **{"from": day.strftime("%Y-%m-%d"), "to": day.strftime("%Y-%m-%d")})
    assert [e["id"] for e in rng["events"]] == [old.id]


def test_a_dismiss_is_a_noise_vote_never_a_second_one(site):
    """#906 re-check: the card's Dismiss after the person's Noise vote adds none; after Useful it makes it Noise."""
    cad, _clock = site
    api = CadApi(cad)
    a, b, c = (save(cad, mode="push", first_at=T0 - i) for i in range(3))
    call(api, f"/api/cad/intel/{a.id}/feedback", {"verdict": "noise"}, JORDAN)
    call(api, f"/api/cad/intel/{b.id}/feedback", {"verdict": "useful"}, JORDAN)
    for e in (a, b, c):
        assert call(api, f"/api/cad/intel/{e.id}/dismiss", {}, JORDAN)[0] == 200
    got = {e.id: sorted(r["verdict"] for r in cad.store.q("SELECT verdict FROM intel_feedback WHERE event_id = ?", (e.id,)))
           for e in (a, b, c)}
    assert got == {a.id: ["noise"], b.id: ["noise"], c.id: ["noise"]}


def test_the_decided_backfill_commits_a_chunk_at_a_time(site, monkeypatch):
    cad, _clock = site
    from sizeup.intel import events as ev_mod
    ids = [save(cad, first_at=T0 - i, rules=[["model.pc", 1, 0.9]] if i % 2 else [["pd.shots", 1, 0.7]]).id for i in range(7)]
    with cad.store.tx() as db:
        db.execute("UPDATE intel_events SET decided = NULL")
    monkeypatch.setattr(ev_mod, "BACKFILL_CHUNK", 3)
    seen = []
    real = cad.store.db.execute
    monkeypatch.setattr(cad.store, "db", type("Db", (), {
        "execute": lambda self, sql, *a: (seen.append(sql.split()[0]), real(sql, *a))[1],
        "executemany": lambda self, sql, rows: real.__self__.executemany(sql, rows),
        "executescript": lambda self, sql: real.__self__.executescript(sql)})())
    ev_mod.SqlEvents(cad.store)
    assert seen.count("BEGIN") == 3 and seen.count("COMMIT") == 3              # 7 rows: three transactions
    monkeypatch.undo()
    got = {r["id"]: r["decided"] for r in cad.store.q("SELECT id, decided FROM intel_events")}
    assert got == {i: ("model" if k % 2 else "rules") for k, i in enumerate(ids)}


def test_a_to_day_ends_at_the_next_local_midnight_on_a_dst_day():
    from sizeup.intel.eventlog import _when
    assert _when("2026-11-01", True) - _when("2026-11-01", False) == 25 * 3600       # the clocks went back
    assert _when("2026-03-08", True) - _when("2026-03-08", False) == 23 * 3600       # ... and forward
    assert _when("2026-11-25", True) - _when("2026-11-25", False) == 24 * 3600


def test_a_poll_reuses_the_count_and_the_facets_until_the_events_change(site, monkeypatch):
    """#906 review M2: the 30 s poll counts again only after a change, and the filters' choices are cached."""
    cad, _clock = site
    for i in range(5):
        save(cad, first_at=T0 - i * 60)
    seen = []
    real = cad.store.q
    monkeypatch.setattr(cad.store, "q", lambda sql, args=(): (seen.append(sql), real(sql, args))[1])
    listing(cad, held="unplaced")
    n0 = len(seen)
    assert any("COUNT(*)" in x for x in seen) and any("GROUP BY" in x for x in seen)
    seen.clear()
    d = listing(cad, held="unplaced", poll=1)
    assert not any("COUNT(*)" in x or "GROUP BY" in x for x in seen) and len(seen) < n0
    save(cad, first_at=T0 - 600, suppressed="unplaced")        # a change: counted and grouped again
    seen.clear()
    d2 = listing(cad, held="unplaced", poll=1)
    assert any("COUNT(*)" in x for x in seen) and any("GROUP BY" in x for x in seen)
    assert (d["total"], d2["total"]) == (0, 1)
    seen.clear()
    cad.intel.events.hit(__import__("sizeup.intel.model", fromlist=["Hit"]).Hit("c9", "pd.shots", 1, 0.7, "weapons", "police", T0))
    listing(cad, held="unplaced", poll=1)
    assert not any("COUNT(*)" in x for x in seen)               # a rule hit is no change to the list


def test_the_listing_walks_the_first_at_index(site):
    cad, _clock = site
    save(cad)
    il = cad.intel_admin.eventlog
    args = {"since": 0, "until": T0 + 1, **il.classes()}
    inner = (f"SELECT e.id, e.first_at, {eventlog.HELD_SQL} AS held, {eventlog.DECIDED_SQL} AS decided FROM intel_events e "
             "WHERE e.first_at >= :since AND e.first_at < :until")
    plan = " ".join(r["detail"] for r in cad.store.q(
        f"EXPLAIN QUERY PLAN SELECT * FROM ({inner}) WHERE held = 'quiet' ORDER BY first_at DESC, id DESC LIMIT 50", args))
    assert "intel_events_first" in plan and "SCAN e " not in plan + " "


# -- feedback ----------------------------------------------------------------------------------------------------------

def test_a_vote_is_one_per_person_and_the_same_one_again_takes_it_back(site):
    """#906 review M1: a double click is one vote; Useful and Noise are one of the two; Should have pushed its own."""
    cad, _clock = site
    e = save(cad, suppressed="unplaced")
    api = CadApi(cad)
    vote = lambda v, who=VAL: call(api, f"/api/cad/intel/{e.id}/feedback", {"verdict": v}, who)[1]   # noqa: E731
    rows = lambda: sorted((r["verdict"], r["operator"].split(" <")[0]) for r in cad.store.q(       # noqa: E731
        "SELECT verdict, operator FROM intel_feedback WHERE event_id = ?", (e.id,)))
    assert vote("useful")["removed"] is False
    assert vote("noise")["removed"] is False                  # replaces Useful
    assert rows() == [("noise", "Val")]
    assert vote("should_push")["removed"] is False and vote("useful", JORDAN)["removed"] is False
    assert rows() == [("noise", "Val"), ("should_push", "Val"), ("useful", "Jordan")]
    assert vote("noise")["removed"] is True                    # the same again: taken back
    assert vote("should_push")["removed"] is True
    assert rows() == [("useful", "Jordan")]
    [row] = listing(cad, who=JORDAN)["events"]
    assert row["votes"] == {"useful": 1} and row["mine"] == ["useful"]
    assert vote("wrong_place")["ok"] and vote("wrong_place")["ok"]           # (the card's other verdicts: as before)
    assert rows().count(("wrong_place", "Val")) == 2


def test_useful_noise_and_should_have_pushed_are_stored_for_tuning(site):
    cad, _clock = site
    e = save(cad, suppressed="unplaced", rules=[["pd.shots", 1, 0.7]], ruleset_hash="abc123")
    api = CadApi(cad)
    assert call(api, f"/api/cad/intel/{e.id}/feedback", {"verdict": "should_push"}, VAL)[0] == 200     # a viewer may vote
    assert call(api, f"/api/cad/intel/{e.id}/feedback", {"verdict": "useful"}, JORDAN)[0] == 200
    assert call(api, f"/api/cad/intel/{e.id}/feedback", {"verdict": "should_push"}, JORDAN)[0] == 200
    assert call(api, f"/api/cad/intel/{e.id}/feedback", {"verdict": "louder"}, VAL)[0] == 400
    rows = cad.store.q("SELECT verdict, operator, rules, ruleset_hash FROM intel_feedback WHERE event_id = ? ORDER BY id",
                       (e.id,))
    val, jordan = "Val <val@example.org>", "Jordan <jordan@example.org>"        # who signed in, not what the page typed
    assert [(r["verdict"], r["operator"]) for r in rows] == [("should_push", val), ("useful", jordan), ("should_push", jordan)]
    assert json.loads(rows[0]["rules"]) == [["pd.shots", 1, 0.7]] and rows[0]["ruleset_hash"] == "abc123"
    [row] = listing(cad, who=VAL)["events"]
    assert row["votes"] == {"should_push": 2, "useful": 1} and row["mine"] == ["should_push"]


def test_more_info_says_why_it_was_held_as_the_page_does(site):
    cad, _clock = site
    a = save(cad, suppressed="out_of_area")
    b = save(cad, mode="map", first_at=T0 - 60)
    api = CadApi(cad)
    held = lambda i: get(api, f"/api/cad/prealerts/intel/{i}", VAL)[1]["outcome"]["held"]       # noqa: E731
    assert held(a.id)["key"] == "out_of_area" and held(a.id)["label"] == "out of area"
    assert held(b.id)["key"] == "mode_rule" and held(b.id)["label"] == "rules map"
    js = (STATIC / "prealert-info.js").read_text()
    assert "held: ${o.held.label}" in js


# -- the page --------------------------------------------------------------------------------------------------------------

def test_the_page_is_in_the_nav_and_linked_from_the_60_control_column():
    html = (STATIC / "cad.html").read_text()
    assert re.search(r'<a id="intel-open" class="tab" href="#intel"', html)
    assert re.search(r'<div class="q-head"><h2[^>]*>60 Control</h2>.*?<a id="q-intel-all"[^>]*href="#intel"', html, re.S)
    assert '<div id="intel-view"' in html and '<script src="/static/intel-page.js"></script>' in html
    js = (STATIC / "cad.js").read_text()
    assert "IntelPage.open(" in js and "IntelPage.close()" in js and '["#intel-open", "intel"]' in js
    assert 'show($("#intel-open"), intelOk)' in js                    # only for a viewer the state gives intel to
    page = (STATIC / "intel-page.js").read_text()
    assert int(re.search(r"POLL_MS = (\d+)", page).group(1)) >= 30000   # nothing per second
    assert page.count("setInterval(") == 1 and "requestAnimationFrame" not in page
    # polled only for the default view's newest page over a short span; else a Refresh; hidden, no timer
    assert "POLL_SPAN_S = 7 * 86400" in page and '"visibilitychange"' in page and 'p.set("poll", "1")' in page
    assert '$("#il-refresh").addEventListener' in page
    assert "/api/cad/intel/list?" in page and "/feedback`" in page


DOM = r"""
const fs = require("fs");
global.window = global;
const IN = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
class CL {
  constructor(e) { this.e = e; }
  _s() { return new Set((this.e.className || "").split(/\s+/).filter(Boolean)); }
  add(c) { const s = this._s(); s.add(c); this.e.className = [...s].join(" "); }
  remove(c) { const s = this._s(); s.delete(c); this.e.className = [...s].join(" "); }
  contains(c) { return this._s().has(c); }
  toggle(c, on) { if (on === undefined ? !this.contains(c) : on) this.add(c); else this.remove(c); }
}
class El {
  constructor(tag, id) { this.tagName = tag.toUpperCase(); this.id = id || ""; this.children = []; this.dataset = {}; this.on = {};
                         this.attrs = {}; this.className = ""; this._t = ""; this.hidden = false; this.title = ""; this.value = "";
                         this.classList = new CL(this); this.disabled = false; }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); }
  get options() { return this.children; }
  get parentElement() { return this._parent || (this._parent = new El("div")); }
  append(...xs) { for (const x of xs) this.children.push(x); }
  replaceChildren(...xs) { this.children = [...xs]; this._t = ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); }
  all() { const r = [this]; for (const c of this.children) if (c && c.all) r.push(...c.all()); return r; }
  find(cls) { return this.all().filter((e) => e.classList.contains(cls)); }
}
global.Option = class extends El { constructor(text, value) { super("option"); this.textContent = text; this.value = value; } };
const ids = {};
global.document = { createElement: (t) => new El(t), hidden: false, readyState: "complete", addEventListener() {} };
global.location = { hash: "" };
global.$ = (sel) => { const id = sel.slice(1); return ids[id] || (ids[id] = new El(id.endsWith("-view") ? "div" : "select", id)); };
global.el = (tag, cls, text) => { const n = new El(tag); if (cls) n.className = cls; if (text !== undefined && text !== null) n.textContent = text; return n; };
global.show = (n, on) => { n.hidden = !on; };
global.setText = (n, t) => { if (n.textContent !== (t ?? "")) n.textContent = t ?? ""; };
global.cad = { scene: { timezone: "America/New_York" } };
global.UI = { toast() {} };
global.setInterval = () => 1; global.clearInterval = () => {};
"""


def run_node(script: str, data: dict) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "in.json"
        inp.write_text(json.dumps(data))
        js = Path(td) / "t.js"
        js.write_text(DOM + script)
        p = subprocess.run([NODE, str(js), str(inp)], capture_output=True, text=True, timeout=30)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout.strip().splitlines()[-1])


@needs_node
def test_the_cards_votes_are_one_click_pressed_and_never_toggled_off_by_a_double_click():
    script = f"""
{(STATIC / "intel.js").read_text()}
const list = el("ol", "pa-list");
let posts = [];
const deps = {{ api: (m, p, b) => {{ posts.push([p, b.verdict]); return Promise.resolve({{ ok: true, removed: false }}); }},
               operator: () => "", time: () => "T", readonly: false, more: false }};
const ev = {{ id: 7, level: "medium", what: 0.7, where: 0.5, precision: "address", rules: [["pd.shots", 1, 0.7]],
             pack_label: "PD", label: "Weapon / shots", town: "Rye", place: "12 Example Road, Rye", confirmed: true,
             first_at: 1, traffic_ids: ["c1"] }};
IntelCard.column(list, {{ open: [ev] }}, deps);
const btns = () => list.children[0].find("aa-acts")[0].children;      // the card's actions (not its play buttons)
const noise = () => btns().find((x) => x.attrs["aria-label"] === "Noise" || x.textContent === "Noise");
const n = noise();
n.on.click[0]({{ stopPropagation() {{}} }});
const off = btns().map((x) => x.disabled);
n.on.click[0]({{ stopPropagation() {{}} }});             // the double click's second
(async () => {{
  await new Promise((r) => setTimeout(r, 10));
  console.log(JSON.stringify({{ posts, off, pressed: noise().attrs["aria-pressed"], again: noise().disabled }}));
}})();
"""
    out = run_node(script, {})
    assert out["posts"] == [["/api/cad/intel/7/feedback", "noise"]]
    assert all(out["off"]) and out["pressed"] == "true" and out["again"] is False


@needs_node
def test_the_page_renders_rows_and_never_redraws_an_unchanged_answer(site):
    cad, _clock = site
    heard(cad, "c1-t0", "shots fired at Sample St and Test Rd", T0)
    save(cad, first_at=T0 - 60, rules=[["model.pc", 1, 0.95]], mode="map", precision="none")
    save(cad, first_at=T0 - 120, suppressed="out_of_area", precision="address", label_full="12 Example Road, Rye",
         lat=41.0, lon=-73.7, town="Rye")
    d = listing(cad)
    script = f"""
{(STATIC / "intel-page.js").read_text()}
const body = $("#il-rows");
IntelPage.render(JSON.parse(JSON.stringify(IN)));
const rows = body.children.slice();
const text = rows.map((r) => r.children.map((c) => c.textContent));
IntelPage.render(JSON.parse(JSON.stringify(IN)));                      // a poll, the same answer: nothing touched
const same = body.children.every((r, i) => r === rows[i]);
const changed = JSON.parse(JSON.stringify(IN));
changed.events[1].votes = {{ useful: 1 }};
IntelPage.render(changed);
const kept = body.children.map((r, i) => r === rows[i]);
rows[2].on.click[0]();
// a vote: every button of its row disabled at the click, before the hub answers (one vote, never two)
let posts = 0;
global.api = () => {{ posts++; return new Promise(() => {{}}); }};
const acts = body.children[0].children[7];
const btns = acts.children.filter((x) => x.tagName === "BUTTON");
btns[0].on.click[0]({{ stopPropagation() {{}} }});
const off = btns.map((x) => x.disabled);
btns[0].on.click[0]({{ stopPropagation() {{}} }});
console.log(JSON.stringify({{ text, same, kept, hash: location.hash, count: $("#il-count").textContent,
                               cats: $("#il-category").options.map((o) => o.value), off, posts }}));
"""
    out = run_node(script, d)
    assert out["same"] is True and out["kept"] == [True, False, True]      # only the changed row rebuilt
    pushed, model, held = out["text"]
    assert pushed[2] == "push" and "Sample St & Test Rd, Rye" in pushed[4] and pushed[6] == "—"
    assert pushed[3] == "MRRS PD" and "pd.shots .75" in pushed[5]
    assert model[2] == "map →push" and model[4] == "unplaced" and "model 95%" in model[5]
    assert model[6] == "uncorroboratedno cue · no rule · unplaced"
    assert held[2] == "log →push" and held[6] == "out of area" and "12 Example Road, Rye ?" in held[4]
    assert out["hash"] == f"#prealert/intel/{d['events'][2]['id']}"
    assert out["count"] == "3 events" and out["cats"] == ["", "weapons"]
    assert out["off"] == [True, True] and out["posts"] == 1
