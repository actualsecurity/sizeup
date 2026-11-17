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
                   + [(2, n, "", PC, LAT + n * 0.00002, LON, 1) for n in range(1, 60, 2)])
    db.executemany("INSERT INTO lines VALUES (?,?,?)",
                   [(1, PC, json.dumps([[LON, LAT], [LON + 0.002, LAT]])),
                    (2, PC, json.dumps([[LON, LAT - 0.002], [LON, LAT + 0.002]]))])
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
    [ev] = fire_calls(sink)
    assert ev.label_full == "40 Example Pl, Port Chester" and ev.mode == "push"
    assert [d.would_push for p, d in sink.pushes if p.category == "fire_call"] == [True]


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
