"""PD dispatch's fire calls as fire pre-alerts (sizeup/intel, the police pack's fire_call): a burning smell or smoke
complaint, the FD being notified, at an address in the source's towns. And the hub runs the intel thread at all.

The shape is a real PD dispatch call-out (a unit number, "can you respond to <number> <street> Place", fillers, "stating
that they smell something burning", "I'll be notifying the FD"); everything in it is invented (this repository is
public): the street (Example Pl, Sample St on a synthetic gazetteer), the unit, the radio ID and the talker."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from sizeup import notify
from sizeup.geo.gazetteer import SCHEMA, Gazetteer
from sizeup.intel import place as place_mod
from sizeup.intel import rules as rules_mod
from sizeup.intel.engine import IntelEngine, RecordingSink
from sizeup.intel.events import MemoryEvents
from sizeup.intel.model import Place
from tests.test_intel import KEY, Clock, row

PC = "Port Chester"
LAT, LON = 41.0, -73.68
TALK = {"talker": "CAR-12", "radio_id": "7000123"}       # invented: a console alias as heard, a radio ID
CALL = ("44. Can you respond to 40 Example Place, uh, over there, uh, stating that they smell something burning "
        "inside the house. I'll be notifying the FD.")


@pytest.fixture(scope="module")
def gaz(tmp_path_factory):
    path = tmp_path_factory.mktemp("gaz") / "synthetic.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", [(1, "example pl", "Example Pl"), (2, "sample st", "Sample St")])
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?)",
                   [(1, n, "", PC, LAT + 0.0001, LON + n * 0.00002, 1) for n in range(2, 80, 2)]
                   + [(2, n, "", PC, LAT + n * 0.0002, LON, 1) for n in range(1, 60, 2)])     # 21: ~470 m north
    db.executemany("INSERT INTO lines VALUES (?,?,?)",
                   [(1, PC, json.dumps([[LON, LAT], [LON + 0.002, LAT]])),
                    (2, PC, json.dumps([[LON, LAT - 0.002], [LON, LAT + 0.012]]))])
    db.execute("INSERT INTO intersections VALUES (?,?,?,?,?)", (1, 2, LAT, LON, PC))
    db.commit()
    db.close()
    return Gazetteer(path)


def engine(gaz=None, locate=None, clock=None):
    sink = RecordingSink()
    e = IntelEngine(settings={}, events=MemoryEvents(), sink=sink, thread=False, clock=clock or Clock(),
                    locate=locate if locate is not None else place_mod.Locator(gaz))
    return e, sink


def fire_calls(sink):
    return [ev for ev, ch in sink.events if ev.category == "fire_call" and ch == "raised"]


# -- the call ---------------------------------------------------------------------------------------------------------

def test_a_burning_smell_with_the_fd_notified_is_a_fire_pre_alert_at_the_address(gaz):
    e, sink = engine(gaz)
    out = e.feed(row("c1-t1", CALL, e.clock(), **TALK))
    [ev] = [x for x in out if x.category == "fire_call"]
    assert ev.label_full == "40 Example Pl, Port Chester" and ev.precision == "address" and ev.town == PC
    assert ev.mode == "push" and not ev.suppressed and ev.what_score >= 0.6
    assert {r[0] for r in ev.rules} >= {"pd.fire_complaint", "pd.fd_notified"}
    [(pev, d)] = [(p, d) for p, d in sink.pushes if p.category == "fire_call"]
    assert d.would_push and d.message["title"] == "PD · Burning smell / FD notified · Port Chester"
    # the words don't confirm the number (no cross street, no read-back): the push says the street and block (push.py);
    # the event, the console and the More info page have the address
    assert d.message["body"].startswith("Example Pl, 0 blk (unconfirmed) · ") and "fire" in d.message["tags"]
    assert "7000123" not in json.dumps(d.message) and "CAR-12" not in json.dumps(d.message)
    assert not [ev for ev, _ch in sink.events if ev.category == "fire_report"]     # one pre-alert, not two


@pytest.mark.parametrize("text", [
    "Respond to 40 Example Place for a burning smell in the apartment.",
    "Can you respond to 40 Example Place, caller reports an odor of smoke, unknown source.",
    "Respond to 40 Example Place, smoke in the hallway. FD is en route.",
    "40 Example Place, they smell smoke, uh, fire department has been notified.",
    "Respond to 40 Example Place, something burning in the kitchen.",
])
def test_the_other_ways_dispatch_says_it(gaz, text):
    e, sink = engine(gaz)
    e.feed(row("c1-t1", text, e.clock(), **TALK))
    [(ev, _d)] = [(p, d) for p, d in sink.pushes if d.would_push]           # one pre-alert ("smoke in the hallway" is
    assert ev.category in ("fire_call", "fire_report")                      # the police pack's fire_report: it wins)
    assert ev.label_full == "40 Example Pl, Port Chester" and ev.mode == "push"


@pytest.mark.parametrize("text", [
    "Respond to 40 Example Place for a fire alarm test, I'll be notifying the FD.",
    "Respond to 40 Example Place, fireworks complaint, they smell something burning.",
    "Respond to 40 Example Place, caller has a burning permit question, smell of smoke from the yard.",
    "Respond to 40 Example Place, a bonfire in the yard, smell of smoke.",
    "Respond to 40 Example Place, neighbor complaining of the smell of marijuana smoke.",
    "Respond to 40 Example Place for a lockout, I'll be notifying the FD.",              # the FD alone: under the threshold
    "Respond to 40 Example Place, burnt food on the stove, a smell of something burning.",
    "40 Example Place, there's no smoke, no odor, FD is clear.",
    "Can you respond to 40 Example Place, caller smells something burning. Disregard, cancel the call.",
])
def test_not_a_fire_call(gaz, text):
    e, sink = engine(gaz)
    e.feed(row("c1-t1", text, e.clock(), **TALK))
    assert fire_calls(sink) == []
    assert not [d for p, d in sink.pushes if p.category == "fire_call"]


# -- the address: required, and ours ------------------------------------------------------------------------------------

def test_no_address_holds_it_at_log_until_the_read_back_places_it(gaz):
    clock = Clock()
    e, sink = engine(gaz, clock=clock)
    e.feed(row("c1-t1", "Caller states they smell something burning, I'll be notifying the FD.", clock(), **TALK))
    [ev] = fire_calls(sink)
    assert ev.mode == "log" and ev.suppressed == "unplaced" and ev.precision == "none"
    assert not any(d.would_push for _p, d in sink.pushes)                           # no address: no push yet
    clock.t += 20
    e.feed(row("c1-t2", "That's 40 Example Place, the burning smell.", clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.label_full == "40 Example Pl, Port Chester" and ev.mode == "push" and ev.suppressed == ""
    [(_p, d)] = [(p, d) for p, d in sink.pushes if d.would_push]
    assert d.message["body"].startswith("Example Pl, ")


def test_a_street_alone_is_no_address(gaz):
    e, sink = engine(locate=lambda text, scope: Place("street", LAT, LON, 300.0, "Example Pl, Port Chester", PC, False, 0.5))
    e.feed(row("c1-t1", CALL, e.clock(), **TALK))
    [ev] = fire_calls(sink)
    assert ev.suppressed == "unplaced" and ev.mode == "log" and not any(d.would_push for _p, d in sink.pushes)


def test_an_address_outside_the_sources_towns_is_held(gaz):
    def elsewhere(text, scope):
        assert scope["towns"] == [PC, "Rye", "Rye Brook"]                             # the pack's towns for the source
        return Place("address", 41.03, -73.62, 25.0, "40 Example Pl, Greenwich", "Greenwich", True, 0.8)
    e, sink = engine(locate=elsewhere)
    e.feed(row("c1-t1", CALL, e.clock(), **TALK))
    [ev] = fire_calls(sink)
    assert ev.suppressed == "out_of_area" and ev.mode == "log" and not any(d.would_push for _p, d in sink.pushes)


def test_the_other_police_categories_still_push_without_a_place(gaz):
    e, sink = engine(locate=lambda text, scope: None)
    e.feed(row("c1-t1", "Shots fired, units respond.", e.clock(), **TALK))
    assert [(p.category, d.would_push) for p, d in sink.pushes] == [("weapons", True)]


# -- where it goes: the pre-alert (intel) topic, never the alerts one ----------------------------------------------------

def test_it_goes_to_the_pre_alert_topic_only(tmp_path, gaz):
    from tests.test_cad import make_cad
    from tests.test_notify import FakeNtfy
    ntfy = FakeNtfy()
    try:
        cad = make_cad(tmp_path)
        p = notify.Pushes(ntfy.url, "pcfd-xmpl-alerts", "pcfd-xmpl-status", intel_topic="pcfd-xmpl-prealert-test",
                          log=lambda m: None)
        cad.intel.notifier, cad.intel.locate = p.notifier(notify.INTEL), place_mod.Locator(gaz)
        cad.comms.record({"id": "c1-t1", "start": cad.clock(), "end": cad.clock() + 17, "source": "MRRS PD",
                          "talkgroup": 9211, "system": "mrrs", "source_key": KEY, "service": "police", "tier": "alert",
                          "unit": "", "text": CALL, "status": "done", "model": "aws-transcribe"})
        cad.intel.drain()
        assert cad.intel.notifier.flush(5)
        [m] = ntfy.got
        assert m["body"]["topic"] == "pcfd-xmpl-prealert-test"
        assert m["body"]["title"] == "PD · Burning smell / FD notified · Port Chester"
        hits = {h["rule_id"] for h in cad.store.q("SELECT rule_id FROM intel_hits WHERE traffic_id = 'c1-t1'")}
        assert hits >= {"pd.fire_complaint", "pd.fd_notified", "pd.dispatch_cue"}
    finally:
        ntfy.close()


# -- the hub runs the engine (it never did: the transcripts waited in its queue, unread) ---------------------------------

def test_the_hub_starts_the_intel_thread_and_reads_context_rows(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    app = HubApp(hub_cfg(tmp_path), log=lambda m: None)
    app.start_layers = app.start_sources = lambda: None        # no map layers or sources: nothing reaches the network
    try:
        app.start()
        e = app.cad.intel
        assert e._thread is not None and e._thread.is_alive()
        e.locate = lambda text, scope: None
        app.cad.comms.record({"id": "c2-t1", "start": app.cad.clock(), "end": app.cad.clock() + 4, "source": "MRRS PD",
                              "talkgroup": 9211, "system": "mrrs", "source_key": KEY, "service": "police",
                              "tier": "alert", "unit": "", "text": "Shots fired, units respond.", "status": "done",
                              "model": "aws-transcribe"})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not app.cad.store.q("SELECT 1 FROM intel_hits WHERE traffic_id = 'c2-t1'"):
            time.sleep(0.05)
        assert app.cad.store.q("SELECT rule_id FROM intel_hits WHERE traffic_id = 'c2-t1' AND rule_id = 'pd.shots'")
    finally:
        app.stop()
    assert e._stop.is_set()                                # its thread ends at its next wait


# -- a held call is placed only by its own words or its read-back (review of #640) --------------------------------------

UNPLACED = "Caller states they smell something burning, I'll be notifying the FD."


def held(gaz, clock):
    e, sink = engine(gaz, clock=clock)
    e.feed(row("c1-t1", UNPLACED, clock(), **TALK))
    [ev] = fire_calls(sink)
    assert ev.suppressed == "unplaced" and ev.mode == "log"
    return e, sink


@pytest.mark.parametrize("text, talk", [
    ("Car 9 respond to 21 Sample Street, I have a neighbor dispute.", TALK),           # the same console, its next job
    ("Car 9, I'm at 21 Sample Street, checking on a house, I have them coming out.", TALK),   # no rule words at all
    ("I'm at 21 Sample Street, I'll check the house, having them come out.", {"talker": "CAR-31", "radio_id": "7000456"}),
])
def test_another_job_never_places_or_pushes_a_held_call(gaz, text, talk):
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += 30
    e.feed(row("c2-t1", text, clock(), **talk))
    [ev] = e.events.open_events()
    assert ev.category == "fire_call" and ev.suppressed == "unplaced" and ev.precision == "none"
    assert "Sample" not in ev.label_full and not any(d.would_push for _p, d in sink.pushes)


def test_the_tie_words_are_the_categorys_own_list(gaz):
    e, _sink = engine(gaz)
    words = e.category_words(e.pack("police"), "fire_call")
    assert {"smell", "burning", "smoke"} <= words
    assert not words & {"have", "having", "come", "check", "being", "been", "something", "start", "send", "home",
                        "house", "building", "store", "room", "request", "route", "fire", "department"}


def test_the_dispatchers_address_only_read_back_after_the_window_places_and_pushes_it(gaz):
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += 150                                        # past the 120 s window: the call-out's hits have aged out
    e.feed(row("c1-t2", "That's going to be 40 Example Place.", clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.label_full == "40 Example Pl, Port Chester" and ev.mode == "push" and ev.suppressed == ""
    [(_p, d)] = [(p, d) for p, d in sink.pushes if d.would_push]
    assert d.message["title"] == "PD · Burning smell / FD notified · Port Chester"


def test_a_read_back_by_another_talker_or_too_late_does_not_place_it(gaz):
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += 30
    e.feed(row("c1-t2", "That's going to be 40 Example Place.", clock(), talker="CAR-31", radio_id="7000456"))
    clock.t += 400
    e.feed(row("c1-t3", "That's going to be 40 Example Place.", clock(), **TALK))      # past RELEASE_S
    [ev] = e.events.open_events()
    assert ev.suppressed == "unplaced" and ev.precision == "none" and not any(d.would_push for _p, d in sink.pushes)


def test_placed_late_by_its_own_words_updates_it_at_log_unpushed(gaz):
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += 400                                        # more than 5 minutes after the call-out
    e.feed(row("c1-t2", "That burning smell is at 40 Example Place, FD is en route.", clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.label_full == "40 Example Pl, Port Chester" and ev.suppressed == "placed_late" and ev.mode == "log"
    assert not any(d.would_push for _p, d in sink.pushes)
    clock.t += 20
    e.feed(row("c1-t3", "40 Example Place, still a smell of something burning, FD en route.", clock(), **TALK))
    assert not any(d.would_push for _p, d in sink.pushes)                            # never pushed after that


# -- one pre-alert per call: fire_report wins over fire_call --------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Respond to 40 Example Place, caller reports a fire in the house.",
    "Respond to 40 Example Place, pot left on the stove, smoke in the apartment.",
    "Respond to 40 Example Place, smoke condition from a chimney, notifying the FD.",
])
def test_one_push_when_both_fire_categories_hit(gaz, text):
    e, sink = engine(gaz)
    e.feed(row("c1-t1", text, e.clock(), **TALK))
    assert [p.category for p, d in sink.pushes if d.would_push] == ["fire_report"]
    assert [ev.category for ev in e.events.open_events()] == ["fire_report"]


def test_a_fire_report_after_a_fire_call_takes_it_over_without_a_second_push(gaz):
    clock = Clock()
    e, sink = engine(gaz, clock=clock)
    e.feed(row("c1-t1", CALL, clock(), **TALK))
    clock.t += 30
    e.feed(row("c1-t2", "40 Example Place, flames showing from the house.", clock(), **TALK))
    assert [ev.category for ev in e.events.open_events()] == ["fire_report"]
    assert [p.category for p, d in sink.pushes if d.would_push] == ["fire_call"]       # the one push, already out
    clock.t += 30
    e.feed(row("c1-t3", "Respond to 40 Example Place, smell of something burning, FD en route.", clock(), **TALK))
    assert len([d for _p, d in sink.pushes if d.would_push]) == 1


def test_a_held_fire_call_taken_over_by_a_fire_report_pushes_once_as_that(gaz):
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += 20
    e.feed(row("c1-t2", "Caller now says there are flames showing.", clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.category == "fire_report"
    assert [p.category for p, d in sink.pushes if d.would_push] == ["fire_report"]


# -- rule edges ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Respond to 40 Example Place, people smoking in the hallway, a smell of smoke.",
    "Respond to 40 Example Place, kids smoking out back, they smell smoke in the hallway.",
    "Respond to 40 Example Place, a car burning rubber doing donuts, smell of burning.",
    "Respond to 40 Example Place, negative smoke, an odor of something burning earlier.",
])
def test_more_not_fire_calls(gaz, text):
    e, sink = engine(gaz)
    e.feed(row("c1-t1", text, e.clock(), **TALK))
    assert fire_calls(sink) == []


def test_a_small_fire_already_out_with_the_fd_notified_still_fires(gaz):
    e, sink = engine(gaz)
    e.feed(row("c1-t1", "Respond to 40 Example Place, a small fire in the kitchen, it's out, FD notified.", e.clock(),
               **TALK))
    assert [p.category for p, d in sink.pushes if d.would_push] == ["fire_call"]


# -- stop() -------------------------------------------------------------------------------------------------------------

def test_stop_ends_the_thread_within_a_second():
    e = IntelEngine(settings={}, events=MemoryEvents(), sink=RecordingSink(), locate=None, clock=Clock())
    e.start()
    assert e._thread.is_alive()
    t0 = time.monotonic()
    e.stop()
    assert time.monotonic() - t0 < 1.5 and not e._thread.is_alive()


def test_no_intel_topic_holds_every_fire_call_push(tmp_path, gaz):
    """#641: no intel topic of its own, nothing is pushed (never the alerts topic); a placed call says "unsent", a held
    one stays "unplaced"."""
    from tests.test_cad import make_cad
    from tests.test_notify import FakeNtfy
    ntfy = FakeNtfy()
    try:
        cad = make_cad(tmp_path)
        p = notify.Pushes(ntfy.url, "pcfd-xmpl-alerts", "pcfd-xmpl-status", log=lambda m: None)
        cad.intel.notifier, cad.intel.locate = p.notifier(notify.INTEL), place_mod.Locator(gaz)
        base = {"start": cad.clock(), "end": cad.clock() + 17, "source": "MRRS PD", "talkgroup": 9211, "system": "mrrs",
                "source_key": KEY, "service": "police", "tier": "alert", "unit": "", "status": "done",
                "model": "aws-transcribe"}
        cad.comms.record({**base, "id": "c1-t1", "text": CALL})
        cad.comms.record({**base, "id": "c3-t1", "source_key": "p25:mrrs:9212", "text": UNPLACED})
        cad.intel.drain()
        cad.intel.notifier.flush(2)
        assert ntfy.got == []
        by = {e.source_key: e for e in cad.intel.events.open_events()}
        assert by[KEY].category == "fire_call" and by[KEY].suppressed == "unsent" and by[KEY].label_full.startswith("40 ")
        assert by["p25:mrrs:9212"].suppressed == "unplaced"
    finally:
        ntfy.close()


# -- re-review of #640: real fire calls the exclusions must not drop; read-backs are the address alone; deferral ----------

@pytest.mark.parametrize("text", [
    "Respond to 40 Example Place, someone reports an outlet smoking, smell of something burning, notifying the FD.",
    "Respond to 40 Example Place, they are reporting the wiring is smoking, burning smell, FD notified.",
    "Respond to 40 Example Place, the donut shop, smell of something burning in the kitchen, notifying the FD.",
    "Respond to 40 Example Place, a toaster on fire in the kitchen.",
])
def test_smoking_wiring_and_a_donut_shop_are_still_fire_calls(gaz, text):
    e, sink = engine(gaz)
    e.feed(row("c1-t1", text, e.clock(), **TALK))
    assert [(p.category, p.label_full) for p, d in sink.pushes if d.would_push] == [("fire_call", "40 Example Pl, Port Chester")]


@pytest.mark.parametrize("text", [
    "Respond to 40 Example Place, people smoking weed in the stairwell, a smell of smoke.",
    "Respond to 40 Example Place, someone smoking a cigarette in the hallway, they smell smoke.",
    "Respond to 40 Example Place, a car doing donuts in the lot, smell of burning.",
])
def test_people_smoking_and_donuts_are_not(gaz, text):
    e, sink = engine(gaz)
    e.feed(row("c1-t1", text, e.clock(), **TALK))
    assert fire_calls(sink) == []


@pytest.mark.parametrize("first, then, at", [
    (UNPLACED, "21 Sample Street, they have a suspicious vehicle in the driveway.", 90),
    ("Car 44, caller states they smell something burning, I'll be notifying the FD.",
     "Car 44 also 21 Sample Street when you clear.", 60),
])
def test_the_dispatchers_next_job_is_no_read_back(gaz, first, then, at):
    clock = Clock()
    e, sink = engine(gaz, clock=clock)
    e.feed(row("c1-t1", first, clock(), **TALK))
    clock.t += at
    e.feed(row("c1-t2", then, clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.suppressed == "unplaced" and ev.precision == "none" and not any(d.would_push for _p, d in sink.pushes)


def test_a_units_address_only_correction_is_a_read_back(gaz):
    clock = Clock()
    e, sink = engine(gaz, clock=clock)
    e.feed(row("c1-t1", "Car 44, caller states they smell something burning, I'll be notifying the FD.", clock(), **TALK))
    clock.t += 40
    e.feed(row("c1-t2", "Car 44, correction, that's 40 Example Place.", clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.label_full == "40 Example Pl, Port Chester" and ev.suppressed == ""
    assert [p.category for p, d in sink.pushes if d.would_push] == ["fire_call"]


def test_an_unplaced_fire_report_takes_the_fire_calls_address(gaz):
    clock = Clock()
    e, sink = engine(gaz, clock=clock)
    e.feed(row("c1-t1", "Report of smoke showing from a building, location unknown.", clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.category == "fire_report" and ev.precision == "none"
    clock.t += 200                                        # past the window: only the open event is left
    e.feed(row("c1-t2", "Car 9 respond to 21 Sample Street, caller smells something burning inside, notifying the FD.",
               clock(), **TALK))
    [ev] = e.events.open_events()                         # no fire_call beside it
    assert ev.category == "fire_report" and ev.label_full.startswith("21 Sample St") and ev.precision == "address"
    pushed = [(p.category, p.precision) for p, d in sink.pushes if d.would_push]
    assert pushed == [("fire_report", "none"), ("fire_report", "address")]       # the place-found push


def test_a_fire_call_elsewhere_is_not_deferred_to_a_placed_fire_report(gaz):
    clock = Clock()
    e, sink = engine(gaz, clock=clock)
    e.feed(row("c1-t1", "Respond to 21 Sample Street, flames showing from the house.", clock(), **TALK))
    clock.t += 200
    e.feed(row("c1-t2", CALL, clock(), **TALK))           # another address: another call
    assert sorted(p.category for p, d in sink.pushes if d.would_push) == ["fire_call", "fire_report"]


# -- follow-up to #640's final review: the building's nouns in a read-back; a short gap for appliance / panel phrases --

@pytest.mark.parametrize("text, at", [
    ("The house is 40 Example Place.", 150),                     # past the 120 s window
    ("Uh, 40 Example Place, the caller's house.", 60),
    ("Copy, that's the apartment building at 40 Example Place.", 45),
])
def test_a_read_back_naming_the_building_places_and_pushes_it(gaz, text, at):
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += at
    e.feed(row("c1-t2", text, clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.label_full == "40 Example Pl, Port Chester" and ev.mode == "push" and ev.suppressed == ""
    assert [p.category for p, d in sink.pushes if d.would_push] == ["fire_call"]


@pytest.mark.parametrize("text, at", [
    ("21 Sample Street, the caller's house, they have a suspicious vehicle.", 90),
    ("Also 21 Sample Street, the caller's apartment, when you clear.", 60),
    ("The house at 21 Sample Street, a vehicle parked on the lawn.", 45),
])
def test_the_building_nouns_never_make_another_job_a_read_back(gaz, text, at):
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += at
    e.feed(row("c1-t2", text, clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.suppressed == "unplaced" and ev.precision == "none" and not any(d.would_push for _p, d in sink.pushes)


def test_known_gap_a_bare_next_address_in_the_window_reads_as_a_read_back(gaz):
    """Documented, not wanted: the dispatcher's next job said as a bare address by the same talker within RELEASE_S is
    word for word a read-back (engine._address_only). An address outside the source's towns would stay held."""
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += 90
    e.feed(row("c1-t2", "21 Sample Street, Port Chester.", clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.label_full.startswith("21 Sample St") and ev.suppressed == ""


def complaint(e, rid="pd.appliance_smoking"):
    [r] = [r for r in e.pack("police").rules if r.id == rid]
    return lambda text: r.hit(text, rules_mod.words(text))


def fire_call_hits(e, text):
    """The fire_call complaint rules (not the FD cue) the text hits."""
    return {rid for rid in ("pd.fire_complaint", "pd.appliance_smoking") if complaint(e, rid)(text)}


@pytest.mark.parametrize("text", [
    "the dryer is smoking",
    "the electrical panel is sparking",
    "a wire is down and sparking",
    "outlet smoking",
    "the wiring is smoking",
    "a toaster on fire",
    "the dryer, it's smoking",
    "the stove was on fire",
    "the dryer in the basement is smoking",                        # a gap of up to 5 words
    "the outlet behind the couch is sparking",
    "wires in the wall are sparking",
    "the electrical panel in the basement is now smoking",
    "the oven in apartment 3 is smoking",
    "the outlet that's behind it is sparking",
    "female caller states the dryer is smoking",                   # a person who reports it: no exclusion
    "a man says his dryer is smoking",
    "the guy's dryer is smoking",
    "someone left the stove on, it's smoking",
    "the man in apartment 3 says his dryer is smoking",
    "the kids left the toaster smoking",                           # an object between the person and the smoke
    "the guy downstairs has his dryer smoking.",
    "male caller says his outlet is sparking",
    "a man reports the outlet in his bedroom is smoking",
    "the man's dryer is smoking",
    "the outlet... it's sparking",                                 # an ellipsis (a hesitation) is no sentence's end
    "the dryer.... smoking",
    "the man by the stove was smoking",                            # contrived: accepted as a fire call (test topic)
    "the woman at the toaster oven is smoking",
])
def test_appliance_and_panel_phrases_still_hit(gaz, text):
    e, _sink = engine(gaz)
    assert complaint(e)(text)


@pytest.mark.parametrize("text", [
    "panel van parked, the driver is smoking in it",
    "panel van, driver smoking",                                  # the gap is short: the person exclusion
    "a panel van parked by the hydrant on the corner, smoking",    # the gap is too long (7 words)
    "the dryer vent was cleaned last week, nobody is smoking",
    "the outlet is not sparking",
    "the dryer isn't smoking anymore",                             # a contraction is a negator
    "the outlet wasn't sparking when the officer got there",
    "two males in the panel truck are smoking",                    # a panel truck is no panel
    "the dryer, a man is smoking",
    "the outlet near the guy who was smoking",
    "the stove. Smoking",                                          # never across a sentence
    "the stove? Smoking",
    "the oven! smoking",
    "the toaster was fine. Kids smoking out back",
    "the man is smoking out front, a smell of smoke",
    "a female smoking on the porch, smells like smoke",
    "the subject was smoking in the car, odor of smoke",
])
def test_appliance_and_panel_phrases_need_the_object_close_to_the_smoke(gaz, text):
    e, _sink = engine(gaz)
    assert fire_call_hits(e, text) == set()


@pytest.mark.parametrize("text, fires", [
    ("Respond to 40 Example Place, the dryer is smoking, notifying the FD.", True),
    ("Respond to 40 Example Place, the electrical panel is sparking, FD notified.", True),
    ("Respond to 40 Example Place, a panel van parked, the driver is smoking in it, notifying the FD.", False),
    # the person exclusion is the appliance rule's own: a person near "smoking" never turns the burning smell off
    ("Respond to 40 Example Place, the kids left the toaster smoking, smell of something burning, notifying the FD.",
     True),
    ("Respond to 40 Example Place, the guy downstairs has his dryer smoking.", True),
])
def test_a_panel_van_with_a_smoking_driver_is_no_fire_call(gaz, text, fires):
    e, sink = engine(gaz)
    e.feed(row("c1-t1", text, e.clock(), **TALK))
    assert [p.category for p, d in sink.pushes if d.would_push] == (["fire_call"] if fires else [])


@pytest.mark.parametrize("text", [
    "The caller's building, 40 Example Place, apartment 3.",
    "That's 40 Example Place, apt 2B.",
])
def test_a_read_back_with_the_apartment_number_places_and_pushes_it(gaz, text):
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += 40
    e.feed(row("c1-t2", text, clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.label_full.startswith("40 Example Pl") and ev.suppressed == ""
    assert [p.category for p, d in sink.pushes if d.would_push] == ["fire_call"]


def test_an_apartment_number_never_lets_another_jobs_words_through(gaz):
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += 40
    e.feed(row("c1-t2", "21 Sample Street, apartment 3, a noise complaint.", clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.suppressed == "unplaced" and not any(d.would_push for _p, d in sink.pushes)


def test_the_person_exclusion_never_turns_the_burning_smell_rule_off(gaz):
    e, _sink = engine(gaz)
    text = "the kids left the toaster smoking, smell of something burning"
    assert fire_call_hits(e, text) == {"pd.fire_complaint", "pd.appliance_smoking"}
    assert fire_call_hits(e, "the guy downstairs has his dryer smoking.") == {"pd.appliance_smoking"}


def test_unit_and_a_number_is_a_police_unit_not_an_apartment(gaz):
    """"Unit 3" is read as a police unit (one the call never named), never as the address's apartment."""
    clock = Clock()
    e, sink = held(gaz, clock)
    clock.t += 40
    e.feed(row("c1-t2", "That's 40 Example Place, unit 3.", clock(), **TALK))
    [ev] = e.events.open_events()
    assert ev.suppressed == "unplaced" and not any(d.would_push for _p, d in sink.pushes)
