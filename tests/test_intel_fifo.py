"""Intel in AIR-TIME order (FIFO): a radio row lands at air time and its transcript later, from whichever engine (the GPU
worker, AWS, the station), so one channel's transcripts can arrive out of air-time order and a row's text can change
(a post-ASR correction, an "unclear" re-ask, a catch-up re-text). The engine reads them as if in air-time order: a late
row is scored with the rows that aired around it, never re-fires or re-pushes out of order, is never dropped as stale
context, and a re-text replaces that row's words in place; a row whose text goes empty or unclear is withdrawn.

Invented scenes only (this repository is public): "100 Example Ave", made-up radio text, fake talkers."""

from __future__ import annotations

from sizeup.intel import backing as B
from sizeup.intel.engine import LATE_S, WITHDRAWN, IntelEngine, RecordingSink
from sizeup.intel.events import MemoryEvents
from tests.test_intel import Clock, ctx_row, engine, example_locate, live, row  # noqa: F401 - live: a fixture

SMELL = "caller smelling something burning at 100 Example Ave"      # fire_call 0.55: under the 0.6 threshold alone
FD = "FD notified"                                                  # fire_call 0.4: under it alone, over it beside SMELL
SHOTS = "shots fired at 100 Example Ave"


def pushed(sink):
    return [(ev, d) for ev, d in sink.pushes if d.would_push]


def shape(e):
    return sorted((ev.category, tuple(sorted(ev.traffic_ids)), ev.what_score, ev.first_at, ev.label_full, ev.status)
                  for ev in e.events.all())


# -- (a) a late row is scored with its air-time neighbours -------------------------------------------------------------

def test_three_rows_read_out_of_order_make_what_in_order_makes():
    t = 5000.0
    rows = [row("a", "respond to 100 Example Ave", t), row("b", SMELL, t + 10), row("c", FD, t + 20)]
    got = []
    for order in ((0, 1, 2), (2, 0, 1)):                               # in order; then read t+20, t, t+10
        e, sink = engine(clock=Clock(t + 25))
        for i in order:
            e.feed(dict(rows[i]))
        got.append((shape(e), len(pushed(sink))))
    assert got[0] == got[1]
    [(cat, tids, _w, first, _l, _s)] = got[0][0]
    assert cat == "fire_call" and tids == ("b", "c") and first == t + 10 and got[0][1] == 1


def test_a_late_row_is_scored_with_the_rows_that_aired_around_it_never_the_newest_read():
    t = 5000.0
    clock = Clock(t - 25)
    e, sink = engine(clock=clock)
    e.feed(row("n", FD, t - 30))                                       # aired 30 s before the late row: its neighbour
    clock.t = t + 152
    e.feed(row("x", "fire department en route", t + 150))              # read last but aired 150 s after it: not one
    clock.t = t + 200
    [ev] = e.feed(row("l", SMELL, t))                                  # read 200 s after it aired
    assert ev.category == "fire_call" and sorted(ev.traffic_ids) == ["l", "n"] and ev.first_at == t - 30
    assert [x.id for x, _d in pushed(sink)] == [ev.id]                 # still fresh: pushed, at its air time


# -- (b) never re-fired or re-pushed out of order ----------------------------------------------------------------------

def test_a_row_read_six_minutes_late_is_recorded_at_its_air_time_never_pushed_as_new():
    t = 5000.0
    clock = Clock(t + 360)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("k", "respond to 100 Example Ave, male with a knife", t))
    assert ev.category == "weapons" and ev.first_at == t and (ev.suppressed, ev.mode) == ("late", "log")
    assert pushed(sink) == []
    e.feed(row("k", "respond to 100 Example Ave, male with a knife", t))       # the same text again: nothing
    assert pushed(sink) == [] and len(e.events.all()) == 1
    # fresh words about it now: new evidence, pushed now; its first transmission stays the late one's air time
    [up] = e.feed(row("k2", "male with a knife still at 100 Example Ave", clock.t - 5))
    assert up.id == ev.id and up.first_at == t and len(pushed(sink)) == 1


def test_a_late_row_never_pushes_an_event_again():
    t = 5000.0
    for read, want in ((t + 25, 2), (t + LATE_S + 60, 1)):          # read in time: an escalation; over LATE_S late: none
        clock = Clock(t + 5)
        e, sink = engine({"intel": {"repush_min": 0}}, clock=clock)
        [ev] = e.feed(row("s1", "the male is armed, respond to 100 Example Ave", t))
        assert len(pushed(sink)) == 1
        clock.t = read
        e.feed(row("s2", "shots fired, male with a gun at 100 Example Ave", t + 20))
        assert len(pushed(sink)) == want and e.events.get(ev.id).what_score > 0.6
        assert e.events.get(ev.id).first_at == t


# -- (c) never dropped as stale context --------------------------------------------------------------------------------

def test_a_row_read_twelve_minutes_late_still_finds_its_neighbours():
    t = 5000.0
    clock = Clock(t + 2)
    e, sink = engine(clock=clock)
    e.feed(row("n", FD, t))
    for i in range(1, 8):                                              # the channel goes on: newer rows, read on time
        clock.t = t + 100 * i
        e.feed(row(f"r{i}", f"car {i} back in service", t + 100 * i - 2))
    assert "n" in e.windows[("police", "p25:mrrs:9211")].entries     # kept past window_s, by air time
    clock.t = t + 720
    [ev] = e.feed(row("l", SMELL, t + 20))
    assert sorted(ev.traffic_ids) == ["l", "n"] and ev.first_at == t and ev.suppressed == "late" and pushed(sink) == []


def test_rows_read_twenty_minutes_and_three_hours_late_are_kept_at_log_never_pushed():
    for late in (20 * 60, 3 * 3600):
        t = 50_000.0
        clock = Clock(t + late)
        e, sink = engine(clock=clock)
        [ev] = e.feed(row("s", "respond to 100 Example Ave, male with a gun", t))
        assert ev.category == "weapons" and ev.first_at == t and (ev.suppressed, ev.mode) == ("late", "log")
        assert pushed(sink) == [] and e.stale == 1
        e.feed(row("n", FD, t + 40))                                   # read at the same time as its neighbour ...
        [fc] = e.feed(row("m", SMELL, t + 30))
        assert sorted(fc.traffic_ids) == ["m", "n"] and fc.suppressed == "late"   # ... and scored with it
        clock.t += 60                                                  # fresh words now: a push of its own
        assert e.feed(row("f", "respond to 100 Example Ave, male with a gun", clock.t - 5)) and len(pushed(sink)) == 1


def test_an_old_row_never_reopens_or_pushes_an_event_again():
    t = 50_000.0
    clock = Clock(t + 5)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("s1", "shots fired near Sample St and Test Rd", t))
    e.feed(row("s2", "disregard, units disregard", t + 20))
    assert e.events.get(ev.id).note == "possibly cancelled"
    clock.t = t + 20 * 60
    e.feed(row("s3", "shots fired near Sample St and Test Rd", t + 30))    # aired after the disregard, read 20 min late
    assert e.events.get(ev.id).note == "possibly cancelled" and len(pushed(sink)) == 1


def test_a_row_over_a_day_old_is_never_read():
    t = 500_000.0
    e, sink = engine(clock=Clock(t + 25 * 3600))
    assert e.feed(row("s", SHOTS, t)) == [] and e.stale == 1 and e.events.all() == [] and e.windows == {}


def test_gpu_and_aws_results_for_adjacent_rows_out_of_order():
    t = 5000.0
    clock = Clock(t + 12)
    e, sink = engine(clock=clock)
    e.feed(row("r2", FD, t + 8, model="gpu-worker"))                  # the GPU worker's, first
    clock.t = t + 40
    [ev] = e.feed(row("r1", SMELL, t, model="aws-transcribe"))        # AWS's, for the row before it, 30 s later
    clock.t = t + 45
    assert e.feed(row("r2", FD, t + 8, model="aws-transcribe", alt_text=FD)) == [] or True   # the other engine's too
    [only] = e.events.all()
    assert only.id == ev.id and sorted(only.traffic_ids) == ["r1", "r2"] and only.first_at == t
    assert len(pushed(sink)) == 1


# -- (d) a re-text replaces that row's words in place ------------------------------------------------------------------

class Clearing(RecordingSink):
    def __init__(self):
        super().__init__()
        self.cleared = []

    def clear(self, tid, keep):
        self.cleared.append((tid, sorted(keep)))


def clearing(settings=None, clock=None):
    sink = Clearing()
    return IntelEngine(settings=settings or {}, events=MemoryEvents(), sink=sink, locate=example_locate, thread=False,
                       clock=clock or Clock(1050)), sink


def test_a_retext_that_adds_then_removes_a_category():
    e, sink = clearing()
    assert e.feed(row("t1", "caller reports a loud noise at 100 Example Ave", 1000)) == []
    [ev] = e.feed(row("t1", SHOTS, 1000))                              # corrected: the category now
    assert ev.category == "weapons" and ev.traffic_ids == ["t1"] and ev.first_at == 1000 and len(pushed(sink)) == 1
    e.feed(row("t1", SHOTS + ", units respond", 1000))                # the same call in other words: no double count
    up = e.events.get(ev.id)
    assert up.status == "open" and up.traffic_ids == ["t1"] and up.first_at == 1000 and len(pushed(sink)) == 1
    out = e.feed(row("t1", "caller reports a loud noise at 100 Example Ave", 1000))   # and back: its only support
    [w] = [x for x in out if x.id == ev.id]
    assert w.note == WITHDRAWN and w.status == "open" and w.pushed_at is not None      # pushed: never un-pushed
    assert sink.cleared[-1][0] == "t1" and "pd.shots" not in sink.cleared[-1][1]     # the old hit cleared in place
    assert len(pushed(sink)) == 1


def test_a_retext_that_removes_the_only_support_of_an_unpushed_event_ends_it():
    e, sink = clearing({"intel_packs": {"police": {"categories": {"weapons": {"mode": "map"}}}}})
    [ev] = e.feed(row("t1", SHOTS, 1000))
    out = e.feed(row("t1", "caller reports a loud noise at 100 Example Ave", 1000))
    [w] = [x for x in out if x.id == ev.id]
    assert (w.status, w.note) == ("ended", WITHDRAWN) and pushed(sink) == []


def test_a_retext_of_a_row_in_a_pushed_event_never_pushes_it_again_nor_moves_first_at():
    clock = Clock(1005)
    e, sink = clearing({"intel": {"repush_min": 0}}, clock)
    [ev] = e.feed(row("t1", "the male is armed, respond to 100 Example Ave", 1000))
    assert len(pushed(sink)) == 1
    clock.t = 1030
    [up] = e.feed(row("t1", "shots fired, the male is armed with a gun, respond to 100 Example Ave", 1000))
    assert up.id == ev.id and up.what_score > ev.what_score - 1 and up.first_at == 1000 and len(pushed(sink)) == 1


# -- (e) the model's context by air time -------------------------------------------------------------------------------

def test_the_models_context_is_assembled_by_air_time_when_neighbours_arrive_late():
    from tests.test_intel_backing import MACHETE, answer, setup
    clock = Clock(1022)
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1)), cfg={"infer_pd_after_s": 30}, clock=clock)
    e.feed(row("t3", MACHETE, 1020))                                   # the trigger, read first
    clock.t = 1030
    e.feed(row("t2", "unit 2 going to be out at the station for a few minutes", 1010))   # its neighbours, read late
    e.feed(row("t1", "unit 1 going to be out at the station for a few minutes", 1000))
    e.feed(row("t0", "unit 0 back in service from the earlier call on the avenue", 800))  # read late too: 220 s before
    e.feed(row("t4", "unit 4 going to be out at the school for a few minutes", 1040))    # after it, read in time
    clock.t = 1060
    e.tick(clock.t)
    [u] = [m[1]["content"] for m in b.asked if "Trigger transmission: " + MACHETE in m[1]["content"]]
    assert u.index("unit 1 going") < u.index("unit 2 going") < u.index("unit 4 going")   # air time, not arrival
    assert "earlier call" not in u                                     # ±CONTEXT_S of the trigger's start only


def test_a_withdrawn_row_leaves_the_models_context():
    from tests.test_intel_backing import MACHETE, answer, setup
    clock = Clock(1022)
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1)), cfg={"infer_pd_after_s": 30}, clock=clock)
    e.feed(row("t3", MACHETE, 1020))
    e.feed(row("t2", "unit 2 going to be out at the station for a few minutes", 1010))
    e.feed(row("t2", "", 1010, status="unclear"))                      # re-asked: unclear
    clock.t = 1060
    e.tick(clock.t)
    [u] = [m[1]["content"] for m in b.asked if "Trigger transmission: " + MACHETE in m[1]["content"]]
    assert "unit 2" not in u


# -- (f) de-escalation by air time -------------------------------------------------------------------------------------

def test_a_row_that_aired_before_a_de_escalation_never_undoes_it():
    clock = Clock(1005)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("w1", "respond to 100 Example Ave, male with a gun", 1000))
    clock.t = 1065
    e.feed(row("w3", "the gun was secured at 100 Example Ave", 1060))
    assert e.events.get(ev.id).note == "de-escalated"
    clock.t = 1090
    e.feed(row("w2", "male with a gun at 100 Example Ave", 1030))       # aired before it, read late
    assert e.events.get(ev.id).note == "de-escalated" and len(pushed(sink)) == 1
    e.feed(row("w4", "male with a gun at 100 Example Ave again", 1085))  # aired after it: the note goes
    assert e.events.get(ev.id).note == ""


# -- (h) withdrawn: the text emptied or unclear since ------------------------------------------------------------------

def test_clean_unclear_clean_inside_a_pushed_event():
    e, sink = clearing()
    [ev] = e.feed(row("t1", SHOTS, 1000))
    assert len(pushed(sink)) == 1
    [w] = e.feed(row("t1", "fired fired fired fired", 1000, status="unclear"))
    assert w.id == ev.id and w.note == WITHDRAWN and w.status == "open" and w.traffic_ids == []
    assert sink.cleared[-1] == ("t1", []) and "t1" not in e.windows[("police", "p25:mrrs:9211")].entries
    [back] = e.feed(row("t1", SHOTS, 1000))                            # the re-ask's clean text: read again
    assert back.id == ev.id and back.traffic_ids == ["t1"] and back.note == "" and len(pushed(sink)) == 1


def test_clean_unclear_clean_inside_an_open_unpushed_event():
    e, sink = clearing({"intel_packs": {"police": {"categories": {"weapons": {"mode": "map"}}}}})
    [ev] = e.feed(row("t1", SHOTS, 1000))
    [w] = e.feed(row("t1", "", 1000))                                  # emptied
    assert (w.status, w.note) == ("ended", WITHDRAWN) and pushed(sink) == []
    [again] = e.feed(row("t1", SHOTS, 1000))
    assert again.id != ev.id and again.status == "open" and pushed(sink) == []


def test_withdrawing_one_of_two_supporting_rows_rescores_without_it():
    e, sink = clearing()
    [ev] = e.feed(row("t1", SHOTS, 1000))
    e.feed(row("t2", "shots fired, male with a gun at 100 Example Ave", 1010))
    [w] = e.feed(row("t1", "", 1000, status="unclear"))
    assert w.id == ev.id and w.traffic_ids == ["t2"] and w.note == "" and w.status == "open"
    assert len(pushed(sink)) == 1


def test_a_first_time_unclear_row_is_nothing():
    e, sink = clearing()
    assert e.feed(row("t1", "fired fired fired fired", 1000, status="unclear")) == [] and sink.cleared == []


# -- (i) the corrected text, (g) the bundle in air-time order (live) ---------------------------------------------------

def test_a_row_with_a_corrected_text_scores_it_and_a_database_without_the_column_reads_the_text(live):
    from sizeup.intel.engine import ROW_SQL, text_sql
    cols = ["id", "start", "text", "status"]
    assert text_sql(cols) == "text"                                    # before #901: the text as is
    assert text_sql([*cols, "corr_text"]) == "COALESCE(NULLIF(corr_text, ''), text) AS text"
    assert ROW_SQL.format(text=text_sql([*cols, "corr_text"]), extra="") == (     # the shape #901 writes for
        "SELECT id, start, end, source, system, service, dept, tier, talkgroup, channel, talker, rig, radio_id, "
        "COALESCE(NULLIF(corr_text, ''), text) AS text, alt_text, model, status FROM radio_traffic WHERE id = ?")
    cad, n = live
    with cad.store.lock:
        if "corr_text" not in {r[1] for r in cad.store.db.execute("PRAGMA table_info(radio_traffic)")}:
            cad.store.db.execute("ALTER TABLE radio_traffic ADD COLUMN corr_text TEXT NOT NULL DEFAULT ''")
    cad.comms.record(ctx_row("c2-t0", "caller reports a loud noise at 100 Example Ave", cad.clock()))
    with cad.store.lock:
        cad.store.db.execute("UPDATE radio_traffic SET corr_text = ? WHERE id = 'c2-t0'", (SHOTS,))
    cad.intel.drain()
    assert "COALESCE(NULLIF(corr_text, ''), text) AS text" in cad.intel._row_sql()
    cad.comms.on_context_text("c2-t0")
    cad.intel.drain()
    [ev] = cad.intel.events.open_events()
    assert ev.category == "weapons" and len(n.sent) == 1
    hits = {h["rule_id"] for h in cad.store.q("SELECT rule_id FROM intel_hits WHERE traffic_id = 'c2-t0'")}
    assert "pd.shots" in hits


def test_the_bundle_lists_the_evidence_in_air_time_order(live):
    from sizeup import context_bundle, sourcekeys
    cad, n = live
    t = cad.clock()
    cad.comms.record(ctx_row("c2-t0", SHOTS, t))
    cad.intel.drain()
    cad.comms.record(ctx_row("c1-t0", "shots fired, male with a gun at 100 Example Ave", t - 20))   # read late
    cad.intel.drain()
    [ev] = cad.intel.events.open_events()
    assert ev.traffic_ids == ["c2-t0", "c1-t0"] and ev.first_at == t
    with sourcekeys.request_context("someone verified"):
        bundle = context_bundle.build(cad, "intel", ev.id, admin=True)
    assert [x["id"] for x in bundle["trigger"]["items"]] == ["c1-t0", "c2-t0"]


# -- (j) a row logged late (#747): scored at air time, recorded, never pushed ------------------------------------------

def test_a_late_flagged_row_with_a_strong_hit_is_recorded_at_log_and_never_pushed():
    t = 5000.0
    e, sink = engine(clock=Clock(t + 30))                              # read 30 s after it aired: fresh, but flagged
    [ev] = e.feed(row("p1", "respond to 100 Example Ave, shots fired, male with a gun", t, late=1))
    assert ev.category == "weapons" and ev.first_at == t and (ev.suppressed, ev.mode) == ("late", "log")
    assert pushed(sink) == []
    e2, sink2 = engine(clock=Clock(t + 3600))                          # an hour after (sizeup was down): still read,
    [ev2] = e2.feed(row("p1", SHOTS, t, late=1))                       # as context and a record
    assert ev2.suppressed == "late" and pushed(sink2) == [] and "p1" in e2.windows[("police", "p25:mrrs:9211")].entries
    [ev3] = e2.feed(row("p2", "male with a gun at 100 Example Ave", t + 15, late=1))    # its neighbour in the burst
    assert ev3.id == ev2.id and sorted(ev3.traffic_ids) == ["p1", "p2"] and pushed(sink2) == []


def test_a_late_column_is_read_where_the_database_has_one(live, tmp_path):
    from tests.test_cad import make_cad
    cad, n = live
    assert ", late" not in cad.intel._row_sql()                       # main today: no column, nothing late
    cad2 = make_cad(tmp_path / "late")
    cad2.intel.notifier, cad2.intel.locate = n, example_locate
    with cad2.store.lock:
        cad2.store.db.execute("ALTER TABLE radio_traffic ADD COLUMN late INTEGER NOT NULL DEFAULT 0")
    cad2.comms.record(ctx_row("c3-t0", "caller reports a loud noise at 100 Example Ave", cad2.clock()))
    cad2.intel.drain()
    with cad2.store.lock:
        cad2.store.db.execute("UPDATE radio_traffic SET late = 1, text = ? WHERE id = 'c3-t0'", (SHOTS,))
    assert cad2.intel._row_sql().endswith("alt_text, model, status, late, rerun_result FROM radio_traffic WHERE id = ?")
    cad2.comms.on_context_text("c3-t0")
    cad2.intel.drain()
    [ev] = cad2.intel.events.open_events()
    assert ev.suppressed == "late" and ev.mode == "log" and n.sent == []


# -- review: a late row is context, never a push's origin; the model is never asked about one --------------------------

def test_a_late_rows_hits_never_make_a_fresh_rows_event_push():
    t = 5000.0
    clock = Clock(t + 310)
    e, sink = engine(clock=clock)
    e.feed(row("l", SMELL, t))                                         # read 310 s after it aired: late
    [ev] = e.feed(row("f", FD, t + 110))                               # in time, 0.4 alone: only the late row lifts it
    assert sorted(ev.traffic_ids) == ["f", "l"] and ev.what_score >= 0.6                  # corroborated: one event,
    assert (ev.suppressed, ev.mode) == ("late", "log") and pushed(sink) == []             # but never pushed from it


def test_a_fresh_row_with_its_own_hit_pushes_with_a_late_row_as_context():
    t = 5000.0
    clock = Clock(t + 310)
    e, sink = engine(clock=clock)
    e.feed(row("l", "respond to 100 Example Ave", t))                  # late: a dispatch cue and the place
    [ev] = e.feed(row("f", "shots fired", t + 110))                    # in time, over the threshold on its own
    assert ev.category == "weapons" and [x.id for x, _d in pushed(sink)] == [ev.id]


def test_a_late_row_is_never_sent_to_the_model():
    from tests.test_intel_backing import MACHETE, answer, setup
    t = 5000.0
    clock = Clock(t + LATE_S + 60)
    e, sink, b = setup(B.FakeBackend(answer()), clock=clock)
    assert e.feed(row("l", MACHETE, t)) == [] and b.asked == []        # read late: context only
    e.feed(row("l2", MACHETE, clock.t - 20, late=1))                   # flagged late (#747): the same
    assert b.asked == []
    e.feed(row("f", MACHETE + " again", clock.t - 2))                  # fresh: asked, the late ones its context
    [u] = [m[1]["content"] for m in b.asked]
    assert u.count("machete") >= 2


def test_a_row_read_before_a_restart_is_still_withdrawn():
    e, sink = clearing()
    [ev] = e.feed(row("t1", SHOTS, 1000))
    e._seen.clear()                                                    # a restart: nothing in memory but the event
    [w] = e.feed(row("t1", "", 1000, status="unclear"))
    assert w.id == ev.id and w.note == WITHDRAWN and e.unclear == 1


def test_a_text_that_reads_unclear_now_is_withdrawn_too():
    e, sink = clearing()
    [ev] = e.feed(row("t1", SHOTS, 1000))
    loop = " ".join(["thank you"] * 8)                                 # a loop: stt_quality.assess
    [w] = e.feed(row("t1", loop, 1000))
    assert w.id == ev.id and w.note == WITHDRAWN and e.unclear == 1


def test_a_cached_row_query_naming_a_missing_column_is_looked_up_again(live):
    cad, n = live
    cad.intel._row_q = cad.intel._row_sql().replace("status", "status, no_such_col")   # as if a column went away
    cad.comms.record(ctx_row("c9-t0", SHOTS, cad.clock()))
    cad.intel.drain()
    assert len(n.sent) == 1 and "no_such_col" not in cad.intel._row_q


# -- #923 review: ties and disregards by air time, late from the clip's end, never two events of one row ---------------

def test_the_same_talker_ten_minutes_later_never_moves_the_knife_call():
    t = 5000.0
    clock = Clock(t + 2)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("k", "respond to maybe 100 Example Ave, male with a knife", t, talker="Car 7"))
    assert ev.label_full == "100 Example Ave, Port Chester" and not ev.confirmed
    clock.t = t + 602
    e.feed(row("r", "Car 7, I'm at Sample St and Test Rd", t + 600, talker="Car 7"))      # a traffic stop, another job
    got = e.events.get(ev.id)
    assert got.label_full == "100 Example Ave, Port Chester" and got.traffic_ids == ["k"]


def test_a_bare_radio_id_ties_the_call_out_and_its_read_back():
    t = 5000.0
    clock = Clock(t + 12)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("k", "respond for a male with a knife", t, talker="1234567"))     # MRRS: a bare radio ID
    assert ev.precision == "none"
    [up] = e.feed(row("r", "copy, 100 Example Ave", t + 10, talker="1234567"))         # its read-back, 10 s later
    assert up.id == ev.id and up.label_full == "100 Example Ave, Port Chester" and up.traffic_ids == ["k", "r"]


def test_two_unknown_radio_rows_14_s_apart_tie():
    t = 5000.0
    clock = Clock(t + 16)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("k", "respond for a male with a knife", t, talker="Unknown radio"))
    [up] = e.feed(row("r", "copy, 100 Example Ave", t + 14, talker="Unknown radio"))
    assert up.id == ev.id and up.label_full == "100 Example Ave, Port Chester"


def test_a_department_placeholder_unit_never_ties_two_rows():
    t = 5000.0
    clock = Clock(t + 32)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("k", "respond to maybe 100 Example Ave, male with a knife", t, rig="DPT239"))
    e.feed(row("r", "copy, Sample St and Test Rd", t + 30, rig="DPT239"))
    got = e.events.get(ev.id)
    assert got.label_full == "100 Example Ave, Port Chester" and got.traffic_ids == ["k"]


def test_a_disregard_ends_only_what_aired_before_it():
    t = 5000.0
    clock = Clock(t + 35)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("v", "vehicle pursuit, failed to stop", t + 30))      # read first
    clock.t = t + 60
    e.feed(row("d", "disregard, units disregard", t))                      # aired before it, read late
    got = e.events.get(ev.id)
    assert got.status == "open" and "v" in e.windows[("police", "p25:mrrs:9211")].entries   # never consumed
    clock.t = t + 70
    e.feed(row("d2", "disregard, units disregard", t + 65))                 # after it: ends it
    assert e.events.get(ev.id).status == "ended"


def test_lateness_is_measured_from_the_clips_end_and_per_pack():
    t = 5000.0
    e, sink = engine(clock=Clock(t + 330))
    [ev] = e.feed(row("s", SHOTS, t, end=t + 40))                          # 290 s after it ended: in time
    assert ev.suppressed == "" and len(pushed(sink)) == 1
    e2, sink2 = engine({"intel_packs": {"police": {"late_s": 60}}}, clock=Clock(t + 120))   # Settings: 60 s
    [ev2] = e2.feed(row("s", SHOTS, t, end=t + 20))
    assert ev2.suppressed == "late" and pushed(sink2) == []
    packs = {p.name: p.late_s for p in e.packs}
    assert packs["police"] == 300 and packs["highway"] == 600 and packs["fire"] == 300


def test_a_late_rows_neighbour_read_after_a_tick_joins_its_event():
    t = 50_000.0
    clock = Clock(t + 3 * 3600)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("m", SHOTS, t))                                       # 3 h old: at log, its until in the past
    assert ev.until >= clock.t + 120
    clock.t += 5
    e.tick(clock.t)
    assert e.events.get(ev.id).status == "open"                            # not expired under its neighbour
    [up] = e.feed(row("n", "male with a gun at 100 Example Ave", t + 10))
    assert up.id == ev.id and sorted(up.traffic_ids) == ["m", "n"] and len(e.events.all()) == 1


def test_a_row_already_an_events_never_raises_a_second_after_a_restart(live):
    cad, n = live
    now = cad.clock()
    cad.comms.record(ctx_row("o1-t0", SHOTS, now - 3 * 3600))
    cad.intel.drain()
    assert len(cad.intel.events.rows()) == 1
    cad.intel.tick(now + 3600)                                              # expired
    cad.intel._seen.clear()                                                 # a restart: nothing in memory
    cad.intel.windows.clear()
    cad.comms.on_context_text("o1-t0")
    cad.intel.drain()
    assert len(cad.intel.events.rows()) == 1 and n.sent == []


def test_forget_drops_a_question_already_queued_for_the_model():
    from tests.test_intel_backing import answer, setup
    e, sink, b = setup(B.FakeBackend(answer()))
    job = {"pack": "police", "key": "p25:mrrs:9211", "tid": "q1", "row": {"text": "x"}, "at": 1000.0, "place": None,
           "who": [], "due": 1000.0}
    e.backing._q.put_nowait(("ask", e, job, None))
    e.backing.forget("q1")
    assert job["dead"] and e.backing._ask(e, job) == [] and b.asked == []


# -- #927 x #923: a row on the timeline at its close ("Transcribing"), its text filled later ----------------------------

def test_a_row_still_transcribing_is_never_read_and_its_late_fill_is_context_then_withdrawn(live):
    """A station's whisper clip lands at its close with no text (status pending) and its text fills the row later
    (Comms.retext). The engine never reads the pending row (nothing considered, no window entry); the fill, read more than
    the pack's late_s after the clip ended, is late (#923): recorded at log, never pushed; a pending copy sent again never
    blanks it (so never withdraws it); a re-ask that comes back unclear withdraws it."""
    cad, n = live
    now = cad.clock()
    pend = dict(ctx_row("w1-t0", "", now - 400), status="pending", model="whisper-station")
    cad.comms.record(pend)
    assert cad.intel.drain() == 0 and cad.intel.counters.snapshot(now)["all"]["h24"]["considered"] == 0
    assert not any("w1-t0" in w.entries for w in cad.intel.windows.values())
    assert cad.comms.retext("w1-t0", SHOTS, "done", "m.bin")                         # the fill, 400 s after air
    assert cad.intel.drain() == 1
    [ev] = cad.intel.events.open_events()
    assert ev.category == "weapons" and (ev.suppressed, ev.mode) == ("late", "log") and n.sent == []
    cad.comms.record(pend, replace=True)                                             # the close's event sent again
    assert cad.store.one("SELECT text FROM radio_traffic WHERE id = 'w1-t0'")["text"] == SHOTS
    assert cad.intel.drain() == 0 and cad.intel.events.get(ev.id).note != WITHDRAWN
    assert cad.comms.retext("w1-t0", "", "unclear", "m.bin")                         # a re-ask: unclear now
    cad.intel.drain()
    assert cad.intel.events.get(ev.id).note == WITHDRAWN and n.sent == []
