"""The intel engine's counters (sizeup/intel/counters.py): what it did with the transcripts it was handed, so "no rule
matched" and "nothing was ever in a pack's scope" read apart, on Settings > System > Intel, on Settings > Station >
Health and in the log. And the `tier` column of the radio log: only another P25 system's rows carry one.

Synthetic only (this repository is public): invented streets, made-up radio text, invented source keys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sizeup.intel import counters as C
from sizeup.intel import engine as engine_mod
from tests.test_health import Rig, station_status
from tests.test_intel import Clock, ctx_row, engine, row
from tests.test_cad import make_cad

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
ELSEWHERE = {"source": "p25:example:1", "system": "example", "service": "fire"}      # in no pack's scope


def pd(e, now=None):
    return next(p for p in e.counts(now)["packs"] if p["pack"] == "police")


# -- what is counted -------------------------------------------------------------------------------------------------

def test_out_of_scope_transcripts_are_counted_apart_from_ones_no_rule_matched():
    clock = Clock()
    e, _sink = engine(clock=clock)
    for i in range(5):
        e.feed(row(f"x{i}", "smoke coming from the house at 100 Example Ave", clock.t, **ELSEWHERE))
    p = pd(e)
    assert p["h24"]["considered"] == 5 and p["h24"]["in_scope"] == 0 and p["h24"]["out_of_scope"] == 5
    assert p["h24"]["matched"] == 0 and p["last_in_scope"] is None
    assert p["text"] == "0 in scope / 5 considered · last in scope: never"
    e.feed(row("t1", "copy that, 10-4", clock.t))                             # in scope, no rule's words
    p = pd(e)
    assert p["h24"]["in_scope"] == 1 and p["h24"]["matched"] == 0 and p["last_in_scope"] == clock.t
    assert p["text"].startswith("1 in scope / 6 considered · last in scope: ")
    allc = e.counts()["all"]["h24"]
    assert (allc["considered"], allc["in_scope"], allc["out_of_scope"]) == (6, 1, 5)


def test_the_age_gate_a_repeat_a_match_below_threshold_an_event_a_push_and_a_held_one():
    clock = Clock()
    e, sink = engine(clock=clock)
    e.feed(row("old", "smoke coming from the house", clock.t - 3600))         # older than max_age_min
    e.feed(row("old2", "smoke coming from the house", clock.t - 3600, **ELSEWHERE))      # too old, but nobody's
    e.feed(row("t1", "respond to 100 Example Ave, smoke coming from the house", clock.t))
    e.feed(row("t1", "respond to 100 Example Ave, smoke coming from the house", clock.t))      # the same text again
    h = pd(e)["h24"]
    assert h["considered"] == 4 and h["in_scope"] == 3 and h["out_of_scope"] == 1
    assert h["too_old"] == 1 and h["repeat"] == 1
    assert h["matched"] == 1 and h["below"] == 0 and h["events"] == 1 and h["pushed"] == 1 and h["held"] == 0
    assert len(sink.pushes) == 1 and e.stale == 2
    clock.t += 600
    e.feed(row("t2", "an odor of smoke", clock.t, source="p25:mrrs:9212"))      # a rule, under the threshold (no cue)
    h = pd(e)["h24"]
    assert h["matched"] == 2 and h["below"] == 1 and h["events"] == 1


def test_held_counts_every_reason_an_event_was_not_pushed_once_per_event():
    clock = Clock()
    e, sink = engine(settings={"intel": {"push": False}}, clock=clock)                 # the kill switch
    e.feed(row("t1", "respond to 100 Example Ave, smoke coming from the house", clock.t))
    clock.t += 20
    e.feed(row("t2", "flames showing at 100 Example Ave", clock.t))                    # the same event again
    h = pd(e)["h24"]
    assert h["events"] == 1 and h["pushed"] == 0 and h["held"] == 1
    e2, _ = engine(settings={"intel_packs": {"police": {"mode": "shadow"}}}, clock=clock)      # shadow: held too
    e2.feed(row("s1", "respond to 100 Example Ave, smoke coming from the house", clock.t))
    assert pd(e2)["h24"]["held"] == 1 and pd(e2)["h24"]["pushed"] == 0


def test_transcripts_read_in_quiet_hours_are_counted():
    clock = Clock()
    e, _sink = engine(settings={"intel": {"quiet_from": "00:00", "quiet_to": "23:59"}}, clock=clock)
    e.feed(row("t1", "respond to 100 Example Ave, smoke coming from the house", clock.t))
    e.feed(row("x1", "anything", clock.t, **ELSEWHERE))
    h = pd(e)["h24"]
    assert h["quiet"] == 1 and h["events"] == 1 and h["pushed"] == 0 and h["held"] == 1


def test_the_rolling_day_forgets_and_since_start_does_not():
    clock = Clock(1_000_000.0)
    e, _sink = engine(clock=clock)
    e.feed(row("x1", "anything", clock.t, **ELSEWHERE))
    clock.t += 25 * 3600
    e.feed(row("x2", "anything", clock.t, **ELSEWHERE))
    p = pd(e)
    assert p["h24"]["considered"] == 1 and p["start"]["considered"] == 2


# -- the line, and its warning ---------------------------------------------------------------------------------------

def test_a_pack_that_is_on_with_nothing_in_scope_for_a_day_is_a_warning():
    clock = Clock(1_000_000.0)
    e, _sink = engine(clock=clock)
    for i in range(4012):
        e.counters.add(clock.t + 23 * 3600, "considered", ["police"])
        e.counters.add(clock.t + 23 * 3600, "out_of_scope", ["police"])
    assert pd(e, clock.t + 23 * 3600)["level"] == "ok"                        # not counted for a day yet
    clock.t += 24 * 3600
    p = pd(e)
    assert p["level"] == "warn" and p["label"] == "PD" and p["on"]
    assert p["text"] == "0 in scope / 4,012 considered · last in scope: never"
    assert "4,012 out of scope" in p["title"] and "Nothing in scope for 24 h" in p["title"]
    e.feed(row("t1", "copy that", clock.t))                                   # one in scope: no warning
    assert pd(e)["level"] == "ok" and "Nothing in scope" not in pd(e)["title"]
    clock.t += 24 * 3600                                                      # ... and a day after it, again
    assert pd(e)["level"] == "warn" and "last in scope: never" not in pd(e)["text"]


def test_detection_or_a_pack_turned_on_is_watched_from_then_not_from_the_start():
    """Off for a day, then on: no warning until it has been on for a day with nothing in scope."""
    for off, on in (({"intel": {"enabled": False}}, {"intel": {}}),
                    ({"intel_packs": {"police": {"mode": "off"}}}, {"intel_packs": {}})):
        clock = Clock(1_000_000.0)
        settings = dict(off)
        e, _sink = engine(settings=settings, clock=clock)
        clock.t += 2 * 86400
        assert pd(e)["level"] == "off"
        settings.clear()
        settings.update(on)
        e.reload()                                                            # (what a change in Settings does)
        assert pd(e)["level"] == "ok" and pd(e)["on"]
        clock.t += 23 * 3600
        assert pd(e)["level"] == "ok"
        clock.t += 3600
        assert pd(e)["level"] == "warn"


def test_an_engine_that_does_not_count_never_warns():
    """A station forwarding to its hub, a standby: the transcripts are read elsewhere. Its line says so, with no
    warning however long it has been up; and one that takes over is watched from then."""
    clock = Clock(1_000_000.0)
    e, _sink = engine(clock=clock)
    e.active = lambda: False                                                  # a station forwarding to a hub
    clock.t += 3 * 86400
    p = pd(e)
    assert p["level"] == "off" and p["text"] == "0 in scope / 0 considered · last in scope: never · not counted here"
    assert "Not counted here" in p["title"] and "Nothing in scope" not in p["title"]
    e2, _ = engine(clock=clock)
    e2.leader = lambda: False                                                 # a standby
    clock.t += 3 * 86400
    assert pd(e2)["level"] == "off" and pd(e2)["text"].endswith(" · not counted here")
    e2.leader = lambda: True                                                  # it takes over: watched from now
    assert pd(e2)["level"] == "ok"
    clock.t += 86400
    assert pd(e2)["level"] == "warn"


def test_a_day_old_row_caught_up_now_is_in_scope_now_and_the_line_agrees_with_itself():
    clock = Clock(1_000_000.0)
    e, _sink = engine(clock=clock)
    clock.t += 30 * 3600
    assert pd(e)["level"] == "warn"
    e.feed(row("late", "copy that", clock.t - 26 * 3600))                     # said 26 hours ago, handed over now
    p = pd(e)
    assert p["h24"]["in_scope"] == 1 and p["h24"]["too_old"] == 1
    assert p["level"] == "ok" and p["last_in_scope"] == clock.t               # never "1 in scope" beside a warning


def test_the_warnings_hover_says_a_quiet_source_looks_the_same():
    clock = Clock(1_000_000.0)
    e, _sink = engine(clock=clock)
    clock.t += 86400
    assert "simply quiet for a day shows this too" in pd(e)["title"]
    assert "can differ from the card's 24 h line" in pd(e)["title"]


def test_a_pack_that_is_off_counts_what_is_in_its_scope_and_no_match():
    clock = Clock()
    e, _sink = engine(settings={"intel_packs": {"police": {"mode": "off"}}}, clock=clock)
    e.feed(row("t1", "respond to 100 Example Ave, smoke coming from the house", clock.t))
    h = pd(e)["h24"]
    assert h["in_scope"] == 1 and h["matched"] == 0 and h["below"] == 0 and h["events"] == 0


@pytest.mark.parametrize("broken", ["add", "in_scope", "watching", "snapshot"])
def test_a_counter_that_fails_never_costs_an_event(broken):
    clock = Clock()
    e, sink = engine(clock=clock)

    def fail(*a, **kw):
        raise RuntimeError("an invented counter fault")
    setattr(e.counters, broken, fail)
    e.reload()
    [ev] = e.feed(row("t1", "respond to 100 Example Ave, smoke coming from the house", clock.t))
    assert ev.category == "fire_report" and len(sink.pushes) == 1 and sink.pushes[0][1].would_push
    assert e.feed(row("old", "smoke coming from the house", clock.t - 3600)) == []
    assert e.feed(row("t1", "respond to 100 Example Ave, smoke coming from the house", clock.t)) == []
    if broken in ("add", "in_scope", "watching"):
        assert e.count_errors > 0


def test_a_pack_that_is_off_is_never_a_warning():
    clock = Clock(1_000_000.0)
    e, _sink = engine(settings={"intel_packs": {"police": {"mode": "off"}}}, clock=clock)
    clock.t += 3 * 86400
    p = pd(e)
    assert p["level"] == "off" and not p["on"] and p["text"].endswith(" · off")
    e2, _ = engine(settings={"intel": {"enabled": False}}, clock=clock)       # detection off: every pack is
    clock.t += 3 * 86400
    assert pd(e2)["level"] == "off"


# -- across a restart, and the hourly line -----------------------------------------------------------------------------

def test_restore_is_idempotent_and_carries_the_day_and_the_last_in_scope():
    clock = Clock(1_000_000.0)
    a = C.Counters(clock)
    a.watching(clock.t, ["police"])
    a.add(clock.t, "considered", ["police"], n=7)
    a.add(clock.t, "in_scope", ["police"], n=2)
    a.in_scope(clock.t, ["police"])
    kept = json.loads(json.dumps(a.state(clock.t, ["police"])))
    clock.t += 1800
    b = C.Counters(clock)
    b.watching(clock.t, ["police"])
    b.restore(kept, clock.t, ["police"])
    b.restore(kept, clock.t, ["police"])                                      # twice: nothing added
    s = b.snapshot(clock.t, ["police"])
    assert s["packs"]["police"]["h24"]["considered"] == 7 and s["packs"]["police"]["start"]["considered"] == 0
    assert s["packs"]["police"]["last_in_scope"] == 1_000_000.0 and s["started"] == clock.t
    assert s["packs"]["police"]["on_since"] == 1_000_000.0                    # watched since before this process
    b.restore({"hours": {"police": {"x": {"considered": "many"}}}, "last_in_scope": {"police": "soon"}, "on_since": True},
              clock.t, ["police"])
    b.restore("nonsense", clock.t, ["police"])
    assert b.snapshot(clock.t, ["police"])["packs"]["police"]["h24"]["considered"] == 7
    clock.t += 30 * 3600                                                      # older than a day: gone
    c = C.Counters(clock)
    c.restore(kept, clock.t, ["police"])
    assert c.snapshot(clock.t, ["police"])["packs"]["police"]["h24"]["considered"] == 0


def test_restore_takes_only_these_packs_the_windows_hours_and_real_counts():
    clock = Clock(1_000_000.0)
    hour = int(clock.t // 3600)
    kept = {"hours": {"police": {str(hour): {"considered": 5, "in_scope": 10 ** 12, "invented": 3},
                                 str(hour + 5): {"considered": 9},            # an hour that has not been
                                 str(hour - 24): {"considered": 9}},          # ... and one out of the window
                      "": {str(hour): {"considered": 5}},
                      **{f"gone{i}": {str(hour): {"considered": 1}} for i in range(3000)}},     # packs that are no more
            "last_in_scope": {"police": clock.t - 60, "gone1": clock.t - 60, "police2": clock.t + 9e9},
            "on_since": {"gone2": 5.0}}
    c = C.Counters(clock)
    c.watching(clock.t, ["police"])
    c.restore(kept, clock.t, ["police"])
    assert set(c.hours) == {"police", ""} and set(c.hours["police"]) == {hour}
    assert c.hours["police"][hour] == {"considered": 5}                       # no count beyond belief, no key not ours
    assert c.last_in_scope == {"police": clock.t - 60} and c.on_since == {"police": clock.t}
    out = c.state(clock.t, ["police"])
    assert set(out["hours"]) == {"police", ""} and len(json.dumps(out)) < 500
    c.add(clock.t, "considered", ["renamed"])                                 # a pack since removed is not kept either
    assert set(c.state(clock.t, ["police"])["hours"]) == {"police", ""}
    clock.t += 5 * 3600
    assert c.snapshot(clock.t, ["police"])["packs"]["police"]["h24"]["considered"] == 5       # the future hour never shows


def test_live_a_kept_state_too_large_to_be_ours_is_refused_and_replaced(tmp_path):
    clock = Clock(1_000_000.0)
    cad = make_cad(tmp_path, clock=clock)
    hour = str(int(clock.t // 3600))
    big = json.dumps({"hours": {f"gone{i}": {hour: {"considered": 1}} for i in range(3000)},
                      "last_in_scope": {"police": clock.t - 60}})
    assert len(big) > C.STATE_MAX
    cad.store.meta_put(engine_mod.COUNTERS_META, big)
    logs = []
    cad.intel.log = logs.append
    clock.t += 60
    cad.intel._maybe_tick()
    assert pd(cad.intel)["last_in_scope"] is None and set(cad.intel.counters.hours) <= {"", "police"}     # started fresh
    assert any("not read, starting fresh" in x for x in logs)
    clock.t += 3600
    cad.intel._maybe_tick()                                                   # the hour's own, small, in its place
    assert len(cad.store.meta_get(engine_mod.COUNTERS_META)) < 1000


def test_live_one_log_line_an_hour_and_nothing_written_before_it(tmp_path):
    clock = Clock(1_000_000.0)
    cad = make_cad(tmp_path, clock=clock)
    logs = []
    eng = cad.intel
    eng.log = logs.append
    eng._restore()                                                            # what start() does, without the thread
    cad.comms.record(ctx_row("c1-t0", "respond to 100 Example Ave, smoke coming from the house", clock.t))
    cad.comms.record(dict(ctx_row("c1-t1", "anything", clock.t), system="example", source_key="p25:example:1"))
    assert eng.drain() == 2
    clock.t += 60
    eng._maybe_tick()
    assert cad.store.meta_get(engine_mod.COUNTERS_META) is None               # nothing kept in the first hour
    assert not [x for x in logs if x.startswith("intel: considered")]
    clock.t += 3600
    eng._maybe_tick()
    assert [x for x in logs if x.startswith("intel: considered")] == ["intel: considered 2, in scope 1, matched 1, events 1"]
    kept = json.loads(cad.store.meta_get(engine_mod.COUNTERS_META))
    assert kept["last_in_scope"]["police"] == 1_000_000.0
    clock.t += 3600
    eng._maybe_tick()                                                         # the next hour: only what is new
    assert logs[-1] == "intel: considered 0, in scope 0, matched 0, events 0"
    # another process (a restart, a handover): the day and the last-in-scope time come back at its first tick as leader
    cad2 = make_cad(tmp_path, clock=clock)
    cad2.intel.log = lambda m: None
    assert pd(cad2.intel)["last_in_scope"] is None
    clock.t += 60
    cad2.intel._maybe_tick()
    p = pd(cad2.intel)
    assert p["last_in_scope"] == 1_000_000.0 and p["h24"]["considered"] == 2 and p["start"]["considered"] == 0


def test_a_standby_neither_logs_nor_keeps_the_counters(tmp_path):
    clock = Clock(1_000_000.0)
    cad = make_cad(tmp_path, clock=clock)
    logs = []
    cad.intel.log, cad.intel.leader = logs.append, lambda: False
    clock.t += 2 * 3600
    cad.intel._maybe_tick()
    assert logs == [] and cad.store.meta_get(engine_mod.COUNTERS_META) is None


# -- where it shows --------------------------------------------------------------------------------------------------

def test_the_intel_page_carries_a_line_per_pack(tmp_path):
    cad = make_cad(tmp_path)
    cad.intel.feed(row("x1", "anything", cad.clock(), **ELSEWHERE))
    d = cad.intel_admin.page(gate=False)
    lines = d["counters"]["packs"]
    assert [x["pack"] for x in lines] == [p["name"] for p in d["packs"]]          # a line for every pack, in its order
    p = next(x for x in lines if x["pack"] == "police")
    assert p["text"] == "0 in scope / 1 considered · last in scope: never"
    assert all(x["h24"]["considered"] == 1 for x in lines)                     # every pack was handed it
    assert set(C.KEYS) <= set(p["h24"]) and set(C.KEYS) <= set(p["start"]) and d["counters"]["all"]["h24"]["considered"] == 1
    json.dumps(d["counters"])


def test_the_health_panel_has_the_packs_lines_and_survives_a_failing_one():
    r = Rig()
    r.at(0, status=station_status())
    assert r.h.panel()["intel"] is None                                       # no engine wired: no section
    clock = Clock(1_000_000.0)
    e, _sink = engine(clock=clock)
    clock.t += 86400
    r.h.intel = lambda: e.counts()["packs"]
    lines = r.h.panel()["intel"]
    assert [x["pack"] for x in lines] == [p.name for p in e.packs]
    line = next(x for x in lines if x["pack"] == "police")
    assert line == {"pack": "police", "label": "PD", "on": True, "level": "warn", "title": line["title"],
                    "text": "0 in scope / 0 considered · last in scope: never"}
    assert r.keys() == []                                                     # a line on the panel, never an alert
    r.h.intel = lambda: 1 / 0
    assert r.h.panel()["intel"][0]["level"] == "warn" and r.h.panel()["health"]["station"]


def test_the_pages_show_the_lines_tersely_with_the_why_in_the_hover():
    health = (STATIC / "health.js").read_text()
    assert 'd.intel' in health and 'p.title = x.title' in health and '"Intel"' in health
    intel = (STATIC / "intel.js").read_text()
    assert "d.counters" in intel and "seen.title = c.title" in intel
    # the "held" count is every event not pushed, whatever the reason (events.py counts(): suppressed != '')
    hover = intel[intel.index("counts.title ="):intel.index("top.append(line(lab")]
    for why in ("shadow", "pushes off", "quiet hours", "a cap", "merged", "scrub", "unsent"):
        assert why in hover, why


# -- the radio log's tier column -------------------------------------------------------------------------------------

def test_only_another_p25_systems_rows_carry_a_tier(tmp_path):
    """The county's talkgroups and the analog channels have no tier of their own (sizeup/tiers.py): their rows' tier
    is "" by design, and nothing fills it in later. A hub that has only ever recorded those has the column empty."""
    cad = make_cad(tmp_path)
    at = cad.clock()
    base = {"start": at, "end": at + 3, "unit": "", "text": "radio check", "status": "done", "model": "whisper"}
    cad.comms.record({**base, "id": "a-1", "source": "FG1", "talkgroup": None})
    cad.comms.record({**base, "id": "b-1", "source": "Fire/EMS 15", "talkgroup": 307})
    cad.comms.record(ctx_row("c-1", "radio check", at))
    tiers = {r["id"]: (r["system"], r["tier"]) for r in cad.store.q("SELECT id, system, tier FROM radio_traffic")}
    assert tiers["a-1"][1] == "" and tiers["b-1"][1] == "" and tiers["c-1"] == ("mrrs", "alert")
    assert set(cad.comms.audio_per_day(1)) == {"alert"}                       # the tiers' storage: the other system's only
    # recorded again without its tier (a caller that doesn't know it): the tier it had stays
    cad.comms.record({k: v for k, v in ctx_row("c-1", "radio check, over", at).items() if k != "tier"})
    r = cad.store.one("SELECT text, tier FROM radio_traffic WHERE id = 'c-1'")
    assert r == {"text": "radio check, over", "tier": "alert"}
    cad.comms.record(dict(ctx_row("c-1", "radio check, over", at), tier="text"))        # ... and a new one is taken
    assert cad.store.one("SELECT tier FROM radio_traffic WHERE id = 'c-1'")["tier"] == "text"
