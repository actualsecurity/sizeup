"""An adapter whose poll() only reads memory is read every poll_s, not every MIN_POLL_S (sizeup/sources/runner.py): a new
event is handed on within its interval, and nothing else gets faster: an adapter that asks its source keeps the 30 s
floor, an unchanged snapshot sends, writes and logs nothing, health() and poll_now() keep the floor's pace, and the
adapter's own first-seen time is the report's. Synthetic data only: FakeFeed (tests/fake_feed.py), made-up events."""

from __future__ import annotations

import threading
import time

import pytest

from sizeup.sources import (ENV_SNAPSHOT, FIRST_SEEN_MAX_S, MIN_POLL_S, SNAPSHOT_MIN_S, SNAPSHOT_RECHECK_S, TRIGGER_MIN_S,
                            FeedEvent, Health)
from sizeup.sources import runner as runner_mod
from sizeup.sources.runner import HEARTBEAT_S, SourceRunner, context_for, every, first_seen, load_adapters, local_poll, pace
from tests.fake_feed import FakeFeed
from tests.test_incident_sources import PAGE, T0, Clock, Rig


class Snapshot(FakeFeed):
    """An adapter that says its poll() only reads memory. asks: the times health() was asked."""
    local_poll = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.asks: list[float] = []

    def health(self) -> Health:
        self.asks.append(self.clock() if self.clock else 0.0)
        return super().health()


def runner(feed, clock, env=None):
    out, logs = [], []
    r = SourceRunner([feed], lambda k, d: out.append((k, d)), log=logs.append, clock=clock, env=env or {})
    return r, r.runs[0], out, logs


def drive(r, run, clock, seconds: float, each=lambda: None) -> int:
    """The thread's loop on the fake clock, no triggers: step, wait what it says. How many steps."""
    end, n = clock.t + seconds, 0
    while clock.t < end:
        each()
        clock.t += r.step(run)
        n += 1
    return n


# -- who is read faster ---------------------------------------------------------------------------------------------


def test_an_adapter_that_asks_its_source_keeps_the_30_s_floor_whatever_it_says():
    clock = Clock()
    feed = FakeFeed(poll_s=5.0, clock=clock)
    r, run, _, _ = runner(feed, clock, env={ENV_SNAPSHOT: "other, another"})      # the switch names other sources
    assert not run.fast and r.step(run) == MIN_POLL_S == 30.0
    drive(r, run, clock, 600)
    assert feed.polls == 21                                    # 10 min: one poll per 30 s, never more
    for junk in (1, "yes", "true", [True], 1.0, None, 0, False):                   # only exactly True lifts the floor
        f = FakeFeed(poll_s=5.0)
        f.local_poll = junk
        assert not local_poll(f) and every(f) == MIN_POLL_S

    class Raises(FakeFeed):
        @property
        def local_poll(self):
            raise RuntimeError("no")
    assert every(Raises(poll_s=5.0)) == MIN_POLL_S
    assert every(FakeFeed(poll_s=45.0)) == 45.0 and every(Snapshot(poll_s=45.0)) == 45.0   # a longer poll_s stands


def test_a_memory_read_adapter_is_read_every_poll_s_down_to_one_second():
    clock = Clock()
    feed = Snapshot(poll_s=5.0, clock=clock)
    r, run, out, _ = runner(feed, clock)
    assert run.fast and r.step(run) == 5.0
    assert every(Snapshot(poll_s=0.01)) == SNAPSHOT_MIN_S == 1.0 and every(Snapshot(poll_s=2)) == 2.0
    # a new incident is handed on at the next read: within poll_s of the adapter having it
    clock.t = T0 + 3
    feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    clock.t = T0 + 5
    r.step(run)
    events = [d for k, d in out if k == "source_event"]
    assert [d["event"]["event_id"] for d in events] == ["A1"] and events[0]["seen_at"] - (T0 + 3) <= feed.poll_s


def test_the_station_s_switch_names_an_adapter_that_does_not_declare_it():
    clock = Clock()
    feed = FakeFeed(poll_s=5.0, clock=clock)                   # as a deployed adapter: no local_poll of its own
    assert runner(feed, clock)[1].fast is False                # off by default
    r, run, _, _ = runner(feed, clock, env={ENV_SNAPSHOT: " county ,other"})
    assert run.fast and r.step(run) == 5.0
    assert SourceRunner([FakeFeed(poll_s=5.0)], lambda k, d: None).runs[0].every == MIN_POLL_S   # os.environ: not set


def test_the_whole_way_a_new_incident_reaches_the_cad_within_the_interval(tmp_path):
    feed = Snapshot(poll_s=2.0)
    rig = Rig(tmp_path, feed)
    run = rig.runner.runs[0]
    assert rig.runner.step(run) == 2.0 and rig.incidents() == []
    rig.at(1.5)
    feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.at(2.0)
    rig.runner.step(run)
    assert len(rig.incidents()) == 1                           # 0.5 s after the adapter had it (was: up to 30 s)


# -- what a read costs when nothing changed --------------------------------------------------------------------------


def test_unchanged_snapshots_send_write_and_log_nothing_more_than_the_floor_did():
    """Ten minutes of 1 s reads beside ten minutes at the floor: the same events, health reports and "seen" lists reach
    the sink (the outbox to the hub, or the CAD's database). The reads in between send nothing at all."""
    counts = {}
    for name, cls in (("floor", FakeFeed), ("fast", Snapshot)):
        clock = Clock()
        feed = cls(poll_s=1.0, clock=clock)
        feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0},
                 comments=[(T0 + 1, "caller reports smoke")])
        r, run, out, logs = runner(feed, clock)
        steps = drive(r, run, clock, 600)
        counts[name] = (steps, [k for k, _ in out], logs)
    assert counts["floor"][0] == 20 and counts["fast"][0] == 600
    assert counts["fast"][1].count("source_event") == 1        # one event, sent once in 600 reads
    assert sorted(counts["fast"][1]) == sorted(counts["floor"][1])
    assert counts["fast"][2] == counts["floor"][2] == ["incident sources: County feed: ok"]   # no line per read


def test_a_change_is_sent_once_and_the_same_event_again_is_not():
    clock = Clock()
    feed = Snapshot(poll_s=1.0, clock=clock)
    feed.put("A1", type="ALARM/COMM", location="12 Example Ave", times={"dispatched": T0})
    r, run, out, _ = runner(feed, clock)
    drive(r, run, clock, 20)
    feed.comment("A1", T0 + 15, "caller reports smoke")
    drive(r, run, clock, 20)
    # the adapter builds its events anew at every read (equal, never the same objects): still nothing sent
    drive(r, run, clock, 20, each=lambda: feed.change("A1", units=()))
    events = [d["event"] for k, d in out if k == "source_event"]
    assert [len(e["comments"]) for e in events] == [0, 1]


def test_health_is_asked_and_sent_at_the_floor_s_pace():
    clock = Clock()
    feed = Snapshot(poll_s=1.0, clock=clock)
    r, run, out, logs = runner(feed, clock)
    drive(r, run, clock, 300)
    assert len(feed.asks) == 10 and all(b - a >= MIN_POLL_S for a, b in zip(feed.asks, feed.asks[1:]))
    assert [k for k, _ in out].count("source_health") == 300 // HEARTBEAT_S     # the first, then a heartbeat a minute
    # a reason that changes at every read ("no list in 31 s", "... 32 s") is one report per 30 s, not one per read
    del out[:], logs[:]
    n = [0]

    def stale():
        n[0] += 1
        feed.outage("stale", f"no list in {n[0]} s")
    drive(r, run, clock, 120, each=stale)
    assert [k for k, _ in out].count("source_health") == 4 and len(logs) == 4
    assert all(d["state"] == "stale" for k, d in out if k == "source_health")


def test_a_poll_that_raises_backs_off_from_the_floor_and_is_failing_no_sooner():
    waits = {}
    for name, cls in (("floor", FakeFeed), ("fast", Snapshot)):
        clock = Clock()
        feed = cls(poll_s=2.0, clock=clock)
        r, run, out, _ = runner(feed, clock)
        r.step(run)
        feed.outage("ok", "", raise_=True)                     # health() still says ok; poll() raises
        got = []
        for _ in range(5):
            clock.t += (w := r.step(run))
            got.append((w, run.health[0]))
        feed.recover()
        clock.t += r.step(run)
        waits[name] = (got, run.health[0], r.step(run))
    assert waits["fast"][0] == waits["floor"][0] == [(30, "backing_off"), (60, "backing_off"), (120, "failing"),
                                                     (240, "failing"), (300, "failing")]
    assert waits["fast"][1] == "ok" and waits["fast"][2] == 2.0 and waits["floor"][2] == 30.0   # recovered: read again


def test_a_bad_event_is_logged_once_per_30_s_not_once_per_read():
    clock = Clock()
    feed = Snapshot(poll_s=1.0, clock=clock)
    feed.put("A1", location="12 Example Ave", times={"dispatched": -1})
    feed.put("B1", location="14 Example Ave", times={"dispatched": T0})
    r, run, out, logs = runner(feed, clock)
    drive(r, run, clock, 90)
    assert [m for m in logs if "skipped" in m] == ["incident sources: County feed: event skipped: times: epoch seconds"] * 3
    assert [d["event"]["event_id"] for k, d in out if k == "source_event"] == ["B1"]
    # an adapter read at the floor logs every one, as before
    clock = Clock()
    slow = FakeFeed(poll_s=30.0, clock=clock)
    slow.put("A1", times={"dispatched": -1})
    slow.put("A2", times={"dispatched": -1})
    r, run, out, logs = runner(slow, clock)
    r.step(run)
    assert len([m for m in logs if "skipped" in m]) == 2


def test_a_read_is_cheap():
    """50 open events of 40 comments each, read 200 times with nothing changed: a few ms a read on any machine (the
    station reads every 1 to 5 s). A loose bound: it catches a read that became expensive, not a slow runner."""
    clock = Clock()
    feed = Snapshot(poll_s=1.0, clock=clock)
    for i in range(50):
        feed.put(f"A{i}", type="ALARM/COMM", location=f"{i} Example Ave", units=("E60", "L31"), times={"dispatched": T0},
                 comments=[(T0 + n, f"comment {n} of a made-up event") for n in range(40)],
                 extra={"jurisdiction": "Sample Village", "run_numbers": {"A": "1"}})
    r, run, out, _ = runner(feed, clock)
    r.step(run)
    del out[:]
    t = time.perf_counter()
    for _ in range(200):
        clock.t += 1
        r.step(run)
    per_read = (time.perf_counter() - t) / 200
    assert per_read < 0.25 and [k for k, _ in out if k == "source_event"] == []


# -- the first-seen time -------------------------------------------------------------------------------------------


def test_the_adapter_s_own_first_seen_time_is_the_report_s():
    clock = Clock()
    feed = Snapshot(poll_s=5.0, clock=clock)
    r, run, out, _ = runner(feed, clock)
    clock.t = T0 + 5
    feed.put("A1", location="12 Example Ave", times={"dispatched": T0}, first_seen=T0 + 1.5)
    r.step(run)
    d = out[0][1]
    assert (d["first_seen"], d["seen_at"]) == (T0 + 1.5, T0 + 5) and "first_seen" not in d["event"]
    # it is not part of the event: a different one later sends nothing, and a real change keeps the first one
    clock.t = T0 + 10
    feed.change("A1", first_seen=T0 + 9)
    r.step(run)
    assert [k for k, _ in out].count("source_event") == 1
    feed.comment("A1", T0 + 11, "caller reports smoke")
    clock.t = T0 + 15
    r.step(run)
    assert [(d["first_seen"], d["seen_at"]) for k, d in out if k == "source_event"][-1] == (T0 + 1.5, T0 + 15)
    assert FeedEvent.from_dict(d["event"]).first_seen is None  # the hub reads it beside the event, never in it


@pytest.mark.parametrize("bad", [None, T0 + 5.001, T0 + 3600, T0 + 5 - FIRST_SEEN_MAX_S - 1, 0, -1, float("nan"),
                                 float("inf"), True, "1760000000", (T0,), (T0 + 5) * 1000])
def test_a_missing_or_unbelievable_first_seen_is_the_hand_off_time(bad):
    """None, later than now, far in the past, zero, not a number, milliseconds: the time the runner got it, and the
    event still goes (a bad stamp never drops an incident)."""
    clock = Clock(T0 + 5)
    feed = FakeFeed(clock=clock)
    r, run, out, logs = runner(feed, clock)
    feed.put("A1", location="12 Example Ave", times={"dispatched": T0}, first_seen=bad)
    r.step(run)
    assert out[0][0] == "source_event" and out[0][1]["first_seen"] == T0 + 5 and not [m for m in logs if "skipped" in m]


def test_first_seen_is_believed_from_now_back_to_the_bound():
    now = T0 + 5
    assert first_seen(FeedEvent("county", "A1", first_seen=now), now) == now
    assert first_seen(FeedEvent("county", "A1", first_seen=now - FIRST_SEEN_MAX_S), now) == now - FIRST_SEEN_MAX_S
    assert first_seen(FeedEvent("county", "A1", first_seen=int(now) - 3), now) == int(now) - 3
    assert first_seen(object(), now) == now                    # an older adapter's event: no such field


def test_the_lead_over_paging_is_measured_from_when_the_feed_had_the_call(tmp_path):
    feed = FakeFeed()
    rig = Rig(tmp_path, feed)
    rig.at(30)                                                 # the hand-off, 28 s after the adapter first had it
    feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60", "L31"), times={"dispatched": T0},
             first_seen=T0 + 2)
    rig.poll()
    rig.at(20)
    rig.page()                                                 # the page: 18 s after the feed had it, before the hand-off
    panel = rig.cad.sources.panel()
    seen = {s["source"]: s for s in panel["incidents"][0]["seen"]}
    assert seen["county"]["first_seen"] == T0 + 2 and seen["county"]["lead_s"] == 18.0
    assert [(x["label"], x["median_s"]) for x in panel["lead"]] == [("County feed", 18.0)]


# -- the log line ----------------------------------------------------------------------------------------------------


def test_the_start_up_line_says_the_interval_really_used(tmp_path, monkeypatch):
    feeds = [FakeFeed("slow", "Slow feed", poll_s=45.0), FakeFeed("quick", "Quick feed", rank=2, poll_s=5.0),
             FakeFeed("named", "Named feed", rank=3, poll_s=5.0), Snapshot("own", "Own feed", rank=4, poll_s=0.5)]
    monkeypatch.setattr(runner_mod, "_factories", lambda env: [("here", lambda: (lambda ctx: feeds))])
    lines = []
    load_adapters(context_for(tmp_path, lines.append), log=lines.append, env={ENV_SNAPSHOT: "named,absent"})
    assert lines == [
        "incident sources: Slow feed (slow, rank 1, every 45 s) from here",
        "incident sources: Quick feed (quick, rank 2, every 30 s (the floor; poll_s 5 s)) from here",
        f"incident sources: Named feed (named, rank 3, memory read every 5 s ({ENV_SNAPSHOT})) from here",
        "incident sources: Own feed (own, rank 4, memory read every 1 s (its local_poll)) from here",
        f"incident sources: {ENV_SNAPSHOT} names 'absent': no such source"]
    for f in feeds:                                            # the line is what the runner does
        assert f"every {SourceRunner([f], lambda k, d: None, env={ENV_SNAPSHOT: 'named'}).runs[0].every:g} s" in pace(
            f, {"named"})


# -- triggers: nothing sooner or oftener toward the source -----------------------------------------------------------


def test_fast_reads_never_move_a_trigger_s_hold():
    """The same scene for an adapter read at the floor and one read every 5 s: the trigger waits just as long. A read
    in between is not "the last poll" the hold is measured from, and step() never calls poll_now()."""
    holds = {}
    for name, cls in (("floor", FakeFeed), ("fast", Snapshot)):
        clock = Clock()
        feed = cls(poll_s=5.0, clock=clock)
        r, run, _, _ = runner(feed, clock)
        r.step(run)                                            # T0: a poll at the floor's pace
        got = []
        for dt in (5, 10, 15, 20, 25, 29, 30, 31):
            clock.t = T0 + dt
            if name == "fast":
                r.step(run)                                    # the reads in between
            if dt == 10:
                assert r.trigger("paging tones") == 1
            got.append(r.hold(run) if run.reason else None)
        holds[name] = got
        assert feed.pokes == [] and run.polled == T0           # (31 s: the waiting trigger's hold is not started over)
        assert r._poll_now(run) is True and feed.pokes == ["paging tones"] and run.polled == T0 + 31
        assert r.trigger("paging tones") == 0                  # one per 30 s, as ever
        assert r.hold(run) == TRIGGER_MIN_S                    # ... and the next one waits from the triggered poll
    assert holds["fast"] == holds["floor"] == [None, 20.0, 15.0, 10.0, 5.0, 1.0, 0.0, 0.0]


def test_after_poll_now_a_memory_read_is_read_again_shortly_and_no_other_adapter_is():
    clock = Clock()
    fast, slow = Snapshot(poll_s=5.0, clock=clock), FakeFeed("other", "Other", poll_s=5.0, clock=clock)
    r = SourceRunner([fast, slow], lambda k, d: None, log=lambda m: None, clock=clock, env={})
    f, s = r.runs
    assert r._next(f, 5.0, True) == SNAPSHOT_RECHECK_S == 2.0 and r._next(f, 5.0, False) == 5.0
    assert r._next(s, 30.0, True) == 30.0                      # one that asks its source: never a second request
    r.step(f)
    clock.t += 12
    r.trigger("paging tones")
    assert r._next(f, 5.0, False) == 5.0 and r.hold(f) == 18.0
    clock.t += 15
    assert r._next(f, 5.0, False) == 3.0                       # ... and no later than the waiting trigger's hold ends


def test_a_trigger_waiting_when_the_adapter_goes_bad_is_dropped():
    """A memory-read adapter is read on while its trigger waits; trouble found meanwhile takes the trigger away, so
    poll_now() never comes during an outage (an adapter read at the floor never polls while one waits)."""
    for trouble in (lambda f: f.outage("backing_off", "HTTP 503"), lambda f: f.outage("ok", "", raise_=True)):
        clock = Clock()
        feed = Snapshot(poll_s=5.0, clock=clock)
        r, run, _, _ = runner(feed, clock)
        r.step(run)
        clock.t += 10
        assert r.trigger("paging tones") == 1 and r._held(run) == 20.0
        trouble(feed)
        clock.t += 25                                          # (health is asked at the floor's pace)
        r.step(run)
        assert r._held(run) == 0.0 and run.reason == "" and r._poll_now(run) is False and feed.pokes == []


def test_the_loop_reads_on_through_a_hold_and_calls_poll_now_once_when_it_ends(monkeypatch):
    """The threads, with the floor and the hold scaled down to 0.6 s: the memory-read adapter is read all through the
    hold, the other is not polled at all; each gets one poll_now(), no sooner than the hold after its last poll."""
    monkeypatch.setattr(runner_mod, "MIN_POLL_S", 0.6)
    monkeypatch.setattr(runner_mod, "TRIGGER_MIN_S", 0.6)
    monkeypatch.setattr(runner_mod, "SNAPSHOT_MIN_S", 0.02)
    monkeypatch.setattr(runner_mod, "SNAPSHOT_RECHECK_S", 0.05)

    class Timed:
        def poll(self):
            self.at.append(("poll", time.monotonic()))
            return super().poll()

        def poll_now(self, reason):
            self.at.append(("now", time.monotonic()))
            super().poll_now(reason)

    class Fast(Timed, Snapshot):
        at: list = []

    class Slow(Timed, FakeFeed):
        at: list = []

    fast, slow = Fast(poll_s=0.02), Slow("other", "Other", poll_s=0.02)
    fast.at, slow.at = [], []
    got = threading.Event()
    r = SourceRunner([fast, slow], lambda k, d: got.set() if k == "source_event" else None, log=lambda m: None, env={})
    r.start()
    try:
        deadline = time.monotonic() + 20
        while not (fast.at and slow.at) and time.monotonic() < deadline:
            time.sleep(0.005)
        asked = time.monotonic()
        assert r.trigger("paging tones") == 2
        while not (fast.pokes and slow.pokes) and time.monotonic() < deadline:
            time.sleep(0.01)
        fast.put("A1", location="12 Example Ave", times={"dispatched": T0})
        assert got.wait(10)                                    # what the triggered fetch brought: read right after
    finally:
        r.stop()
    assert fast.pokes == ["paging tones"] and slow.pokes == ["paging tones"]
    for feed in (fast, slow):
        now = next(i for i, (k, _) in enumerate(feed.at) if k == "now")
        polls = [t for k, t in feed.at[:now] if k == "poll"]
        held = [t for t in polls if t > asked]                 # between the trigger and its poll_now()
        if feed is slow:
            assert held == [] and feed.at[now][1] - polls[-1] >= 0.55     # it waited out the hold, no poll meanwhile
        else:
            assert held and feed.at[now][1] - polls[0] >= 0.55            # read on through the hold; told no sooner
