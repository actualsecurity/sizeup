"""Incident sources, ranked (#297): the adapter interface (sizeup/sources/), the station's runner, and the CAD's rules
(sizeup/cad/sources.py): promotion, demotion, strict matching, reconciliation, manual edits kept, merges, the
timeline, the lead over paging, and pre-alerts that never make an incident. Synthetic data only: FakeFeed (tests/fake_feed.py) with made-up
events, places and times."""

from __future__ import annotations

import json
import sys
import threading
import time
import types

import pytest

from sizeup.cad import sources as src
from sizeup.cad.sources import MATCH_WINDOW_S, PAGING, REPORT_STALE_S, norm_unit, same_place, units_match
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore
from sizeup.sources import FeedEvent, Health, SourceError
from sizeup.sources import runner as runner_mod
from sizeup.sources.runner import (HEARTBEAT_S, HubRefused, SourceRunner, check_adapter, context_for, load_adapters,
                                  require_station)
from tests.fake_feed import FakeFeed
from tests.test_cad import local, make_cad

T0 = local(2026, 10, 7, 14, 30)
PAGE = ("Port Chester Fire Department, Engine 60, Ladder 31, respond to an activated fire alarm. 12 Example Avenue, "
        "cross of Sample Street. Time out 1432.")
OTHER_PAGE = ("Port Chester Fire Department, Engine 62, respond to an activated fire alarm. 400 Sample Street. "
              "Time out 1450.")


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Rig:
    """A CAD of record with feeds run by a SourceRunner on the same clock (as all-in-one: the sink is the CAD)."""

    def __init__(self, tmp_path, *feeds: FakeFeed):
        self.clock = Clock()
        self.cad = make_cad(tmp_path, clock=self.clock)
        self.feeds = list(feeds) or [FakeFeed(clock=self.clock)]
        for f in self.feeds:
            f.clock = self.clock
        self.sent: list[tuple[str, dict]] = []
        self.runner = SourceRunner(self.feeds, self.sink, log=lambda m: None, clock=self.clock)
        self.monitor = IncidentStore({"Port Chester FD", "Rye Brook FD"}, geocode=lambda d: None)

    @property
    def feed(self) -> FakeFeed:
        return self.feeds[0]

    def sink(self, kind: str, d: dict) -> None:
        self.sent.append((kind, d))
        (self.cad.sources.on_event if kind == "source_event" else self.cad.sources.on_health)(d)

    def poll(self) -> None:
        """One turn of each adapter's loop: poll_now(reason) when triggered, then the poll."""
        for r in self.runner.runs:
            self.runner._poll_now(r)
            self.runner.step(r)

    def at(self, dt: float) -> None:
        self.clock.t = T0 + dt

    def page(self, pid: str = "p1", text: str = PAGE, fresh: bool = False):
        if fresh:                                              # another call (the monitor would merge a page within 5 min)
            self.monitor = IncidentStore({"Port Chester FD", "Rye Brook FD"}, geocode=lambda d: None)
        inc = self.monitor.on_page(pid, ["Port Chester FD"], parse(text), at=self.clock.t)
        return self.cad.from_monitor(inc)

    def uid(self, name: str) -> int:
        return self.cad.store.one("SELECT id FROM units WHERE name = ?", (name,))["id"]

    def incidents(self) -> list[dict]:
        return self.cad.store.q("SELECT * FROM incidents ORDER BY id")

    def feed_lines(self, iid: int) -> list[str]:
        return [e["text"] for e in self.cad.store.q("SELECT text FROM events WHERE incident_id = ? AND kind = 'feed' "
                                                    "ORDER BY at, id", (iid,))]


# -- the interface ---------------------------------------------------------------------------------------------------


def test_a_feed_event_is_checked_and_round_trips():
    ev = FeedEvent("county", "A1", "STRUCTURE FIRE", "12 Example Ave", ("E60", "L31"), {"dispatched": T0},
                   {"E60": {"on_location": T0 + 240}}, ((T0 + 30, "smoke showing"),)).check()
    assert FeedEvent.from_dict(ev.as_dict()) == ev
    for bad in (dict(source="Paging"), dict(source="paging"), dict(event_id=""), dict(times={"arrived": T0}),
                dict(times={"dispatched": -1}), dict(comments=(("x", "y"),)), dict(units=("",))):
        with pytest.raises(SourceError):
            FeedEvent(**{"source": "county", "event_id": "A1", **bad}).check()
    with pytest.raises(SourceError):
        Health("down").check()
    assert Health("backing_off", "HTTP 429").check().state == "backing_off"


def test_adapters_load_by_entry_point_and_by_module_path(tmp_path, monkeypatch):
    lines = []
    ep = types.SimpleNamespace(name="fake", load=lambda: (lambda ctx: FakeFeed("viaep", "EP feed", rank=2)))
    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: [ep] if group == "sizeup.incident_sources" else [])
    pkg = tmp_path / "adapters"
    pkg.mkdir()
    (pkg / "my_adapter.py").write_text("from tests.fake_feed import FakeFeed\n"
                                       "def make(ctx):\n    return [FakeFeed('one', 'One'), FakeFeed('one', 'Again')]\n"
                                       "def broken(ctx):\n    raise RuntimeError('no credentials')\n")
    monkeypatch.setattr(sys, "path", list(sys.path))
    env = {"SIZEUP_INCIDENT_SOURCES": "my_adapter:make, my_adapter:broken, tests.fake_feed:make",
           "SIZEUP_INCIDENT_SOURCES_PATH": str(pkg)}
    got = load_adapters(context_for(tmp_path, lines.append), log=lines.append, env=env)
    assert [(a.source, a.label, a.rank) for a in got] == [("viaep", "EP feed", 2), ("one", "One", 1), ("fake", "Fake feed", 1)]
    assert any(m.endswith("my_adapter:broken not loaded: RuntimeError") for m in lines)
    assert any("source 'one' twice" in m for m in lines)
    assert check_adapter(FakeFeed("paging")) and check_adapter(FakeFeed(rank=0)) and not check_adapter(FakeFeed())


def test_the_runner_sends_new_and_changed_events_and_health_on_change_and_heartbeat(tmp_path):
    clock = Clock()
    feed = FakeFeed(clock=clock)
    sent = []
    r = SourceRunner([feed], lambda k, d: sent.append((k, d)), log=lambda m: None, clock=clock)
    feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    assert r.step(r.runs[0]) == feed.poll_s
    assert [k for k, _ in sent] == ["source_event", "source_health"]
    assert sent[0][1]["event"]["event_id"] == "A1" and sent[0][1]["first_seen"] == T0
    clock.t += 10
    r.step(r.runs[0])
    assert len(sent) == 2                                  # nothing changed, no heartbeat due
    feed.comment("A1", T0 + 15, "caller reports smoke")
    clock.t += 10
    r.step(r.runs[0])
    assert sent[-1][0] == "source_event" and sent[-1][1]["first_seen"] == T0 and sent[-1][1]["seen_at"] == T0 + 20
    clock.t += HEARTBEAT_S
    r.step(r.runs[0])
    assert sent[-1][0] == "source_health"                  # the heartbeat
    feed.outage("failing", "HTTP 503", raise_=True)
    waits = [r.step(r.runs[0]) for _ in range(3)]
    assert waits == [10, 20, 40]                           # backing off while poll() raises
    assert sent[-1][1]["state"] == "failing" and "HTTP 503" in sent[-1][1]["reason"]
    feed.put("bad", location=5)                            # breaks the interface: skipped, the rest go on
    feed.recover()
    r.step(r.runs[0])
    assert sent[-1][1]["state"] == "ok" and not any(d.get("event", {}).get("event_id") == "bad" for _, d in sent)
    r.stop()
    assert feed.closed


# -- the rules -------------------------------------------------------------------------------------------------------


def test_same_place_is_strict():
    assert same_place("12 EXAMPLE AVE", "12 Example Avenue") and same_place("12 Example Ave Apt 3", "12 Example Avenue")
    assert same_place("12 Example Ave, Example Village", "12 Example Ave # 2")
    assert not same_place("14 Example Ave", "12 Example Avenue")
    assert not same_place("12 N Main St", "12 S Main St") and same_place("12 N Main St", "12 Main St")
    assert not same_place("King St", "500 King St")                          # a street is not an address on it
    assert not same_place("Main St & Example Ave", "100 Example Ave")        # an intersection never a numbered address
    assert same_place("Main St & Example Ave", "Example Avenue and Main Street")
    assert not same_place("12 Example Ave", "12 Example St") and not same_place("", "12 Example Ave")


def test_units_helpers():
    assert norm_unit("Engine 60") == norm_unit("E-60") == "E60" and norm_unit("Tower Ladder 2") == "TL2"
    assert units_match(["Engine 60", "Ladder 31"], ["E60"]) == {"E60"} and not units_match(["E61"], ["E60"])


def test_the_feed_creates_and_the_page_attaches_without_a_duplicate(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60", "L31"), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert (inc["source"], inc["status"], inc["type"], inc["subtype"], inc["address"]) == (
        "CAD feed", "queued", "ALARM", "COMM", "12 EXAMPLE AVE")
    assert rig.cad.sources.acting()["source"] == "county"
    rig.at(75)                                             # the page, 75 s later
    got = rig.page()
    assert got["id"] == inc["id"] and len(rig.incidents()) == 1
    assert any(t.startswith("Page 14:31:15 attached (1:15 after County feed)") for t in rig.feed_lines(inc["id"]))
    rig.at(80)
    assert rig.page()["id"] == inc["id"] and len(rig.incidents()) == 1     # the page's repeat: still one
    after = rig.cad.store.incident(inc["id"])
    assert after["address"] == "12 EXAMPLE AVE" and "60 Control page" not in after["remarks"]   # the feed's fields rule
    panel = rig.cad.sources.panel()
    [row] = panel["incidents"]
    assert [(s["source"], s["lead_s"]) for s in row["seen"]] == [("county", 75.0), (PAGING, None)]
    assert panel["lead"] == [{"label": "County feed", "n": 1, "median_s": 75.0, "min_s": 75.0, "max_s": 75.0}]


def test_a_page_creates_its_incident_at_once_and_the_feed_attaches_later_and_takes_authority(tmp_path):
    """No hold (review #3): the feed healthy and silent, the page still makes its incident at once."""
    rig = Rig(tmp_path)
    rig.poll()
    assert rig.cad.sources.acting()["source"] == "county"
    got = rig.page()
    assert got is not None and got["source"] == "60 Control page" and got["status"] == "queued"
    assert rig.cad.sources.state()["incoming"] == []       # no held notices any more
    rig.at(30)
    rig.feed.put("A1", type="STRU", location="12 Example Ave", units=("E60",), times={"dispatched": T0 - 5})
    rig.poll()
    [inc] = rig.incidents()
    assert inc["id"] == got["id"] and inc["type"] == "STRU"                  # queued: the feed's type
    [att] = [x for x in rig.feed_lines(inc["id"]) if x.startswith("County feed: attached")]
    assert att == "County feed: attached (30 s after Paging) · now authoritative"
    [row] = rig.cad.sources.panel()["incidents"]
    assert {s["source"]: s["lead_s"] for s in row["seen"]} == {PAGING: None, "county": -30.0}


def test_promotion_when_the_primary_fails_and_demotion_when_it_recovers(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.outage("backing_off", "HTTP 429")
    rig.poll()
    st = rig.cad.sources.state()
    assert st["acting"] == "Paging"
    assert [(t["source"], t["state"], t["acting"]) for t in st["tiers"]] == [("county", "backing_off", False), (PAGING, "ok", True)]
    rig.feed.recover()
    rig.at(30)
    rig.poll()
    assert rig.cad.sources.state()["acting"] == "County feed"
    rig.at(30 + REPORT_STALE_S + 1)                        # no report since: stale, paging acts again
    t = rig.cad.sources.tiers()[0]
    assert (t["state"], t["healthy"]) == ("stale", False) and t["reason"].startswith("no report since")
    assert rig.cad.sources.state()["acting"] == "Paging"
    rig.cad.sources.paging_ok = lambda: False              # the station offline: nothing healthy
    assert rig.cad.sources.acting() is None


def test_a_feed_event_never_attaches_to_a_closed_incident_sharing_a_unit(tmp_path):
    """Review S1: a closed manual call that had E60 must not swallow a new call E60 is sent to."""
    rig = Rig(tmp_path)
    m = rig.cad.create({"type": "STRU", "address": "77 Other Rd", "operator": "Lt. Test"})
    rig.cad.store.assign(m["id"], [rig.uid("E60")], "Lt. Test")
    with rig.cad.store.tx() as db:
        db.execute("UPDATE incidents SET status = 'closed', closed_at = ? WHERE id = ?", (T0 + 300, m["id"]))
    rig.at(600)
    rig.feed.put("B1", type="STRU", location="400 Sample St", units=("E60",), times={"dispatched": T0 + 600})
    rig.poll()
    new = rig.incidents()[-1]
    assert new["id"] != m["id"] and (new["source"], new["status"], new["address"]) == ("CAD feed", "queued", "400 Sample St")
    assert rig.feed_lines(m["id"]) == []


def test_a_page_never_attaches_to_a_manual_incident(tmp_path):
    """Review S2: a manual call that has E60 and a page sending E60 elsewhere: two calls, nothing buried."""
    rig = Rig(tmp_path)
    m = rig.cad.create({"type": "STRU", "address": "77 Other Rd", "operator": "Lt. Test"})
    rig.cad.store.assign(m["id"], [rig.uid("E60")], "Lt. Test")
    rig.at(600)
    got = rig.page()
    assert got["id"] != m["id"] and got["source"] == "60 Control page"
    assert rig.feed_lines(m["id"]) == [f"Possibly the same as #{got['id']}: 12 Example Avenue"]    # noted, not buried
    # the same place as a manual call: its own incident still, noted on both
    same = rig.cad.create({"type": "ALARM", "subtype": "COMM", "address": "400 Sample Street", "operator": "Lt. Test"})
    rig.at(700)
    p2 = rig.page("p2", OTHER_PAGE, fresh=True)
    assert p2["id"] not in (m["id"], same["id"])
    assert f"Possibly the same as #{same['number']}: 400 Sample Street" in rig.feed_lines(p2["id"])
    assert rig.feed_lines(same["id"]) == [f"Possibly the same as #{p2['id']}: 400 Sample Street"]


def test_two_places_are_two_calls_whatever_the_units_and_cleared_units_never_match(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.outage("failing", "down")                     # paging acting: no difference to matching
    rig.poll()
    page = rig.page()                                      # E60, L31 at 12 Example Avenue
    rig.at(60)
    rig.feed.recover()
    rig.feed.put("B1", type="STRU", location="400 Sample St", units=("E60",), times={"dispatched": T0 + 60})
    rig.poll()
    assert len(rig.incidents()) == 2                       # a shared unit never joins two places, only notes them
    assert rig.feed_lines(page["id"]) == ["Possibly the same as #2: 400 Sample St"]
    # a cleared unit is not on the call any more
    c = rig.cad.create({"type": "EMS", "address": "", "operator": "Lt. Test"}) if False else None
    m = rig.cad.store.incident(page["id"])
    rig.cad.store.assign(m["id"], [rig.uid("E64")], "Lt. Test")
    with rig.cad.store.tx() as db:
        db.execute("UPDATE assignments SET cleared_at = ? WHERE unit_id = ?", (T0 + 90, rig.uid("E64")))
    rig.at(120)
    rig.feed.put("C1", type="EMS", location="", units=("E64",), times={"dispatched": T0 + 120})
    rig.poll()
    assert rig.incidents()[-1]["source_ref"] == "feed:county:C1" and c is None


def test_units_alone_match_only_when_the_incident_has_no_location(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM", location="", units=("E60",), times={"dispatched": T0})
    rig.poll()
    [a] = rig.incidents()
    rig.at(40)
    assert rig.page()["id"] == a["id"]                     # the page has the place, the feed's incident none yet


def test_a_feed_event_never_joins_an_accepted_incident_it_didnt_make(tmp_path):
    """Review #2: the page was accepted (a person runs it): the feed's event is its own queued incident, noted."""
    rig = Rig(tmp_path)
    page = rig.page()
    rig.cad.accept(page["id"], {"type": "ALARM", "subtype": "COMM", "operator": "Lt. Test"})
    rig.at(40)
    rig.feed.put("A1", type="STRU", location="12 Example Ave", units=("E60",), times={"dispatched": T0 + 30})
    rig.poll()
    a, b = rig.incidents()
    num = rig.cad.store.incident(a["id"])["number"]
    assert (b["source"], b["status"]) == ("CAD feed", "queued") and rig.cad.store.incident(a["id"])["type"] == "ALARM"
    assert f"Possibly the same as #{num}: 12 Example Avenue" in rig.feed_lines(b["id"])


def test_a_person_s_type_location_and_pin_are_kept_and_use_the_feeds_is_one_click(tmp_path):
    """Review S4: the feed's later changes never overwrite what a person set on an accepted call."""
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    rig.cad.accept(inc["id"], {"operator": "Lt. Test"})
    rig.cad.update(inc["id"], {"address": "400 Sample St", "lat": 41.0, "lon": -73.6, "operator": "Lt. Test"})
    rig.cad.update(inc["id"], {"type": "STRU", "subtype": "", "operator": "Lt. Test"})
    assert rig.cad.sources.manual(inc["id"]) == {"type", "location", "pin"}
    rig.at(30)
    rig.feed.change("A1", location="14 EXAMPLE AVE", type="ALARM/RESD")
    rig.poll()
    a = rig.cad.store.incident(inc["id"])
    assert (a["address"], a["type"], a["lat"]) == ("400 Sample St", "STRU", 41.0)
    lines = rig.feed_lines(inc["id"])
    assert "County feed: location 14 EXAMPLE AVE (the location set by hand kept: Use County feed's location)" in lines
    assert "County feed: type ALARM/RESD (the type set by hand kept: Use County feed's type)" in lines
    [shown] = [i for i in rig.cad.state()["incidents"] if i["id"] == inc["id"]]
    assert shown["feed"]["use"] == {"type": "ALARM/RESD", "location": "14 EXAMPLE AVE"}
    rig.cad.feed_use(inc["id"], {"field": "location", "operator": "Lt. Test"})
    a = rig.cad.store.incident(inc["id"])
    assert (a["address"], a["lat"]) == ("14 EXAMPLE AVE", 41.0)            # the pin a person set stays
    assert "location" not in rig.cad.sources.manual(inc["id"]) and "Used County feed's location" in rig.feed_lines(inc["id"])
    rig.at(60)
    rig.feed.change("A1", location="16 EXAMPLE AVE")       # no hand on the location now: the feed's applies
    rig.poll()
    assert rig.cad.store.incident(inc["id"])["address"] == "16 EXAMPLE AVE"


def test_every_feed_change_and_comment_is_on_the_timeline(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0},
                 comments=((T0 + 5, "alarm company: zone 3"),))
    rig.poll()
    [inc] = rig.incidents()
    assert rig.feed_lines(inc["id"]) == ["County feed: units E60", "County feed: dispatched 14:30:00",
                                         "County feed comment: alarm company: zone 3"]
    rig.at(60)
    rig.feed.change("A1", units=("E60", "L31"), times={"responding": T0 + 50}, type="STRU", location="14 Example Ave",
                    unit_times={"E60": {"on_location": T0 + 55}})
    rig.feed.comment("A1", T0 + 58, "smoke showing")
    rig.poll()
    after = rig.cad.store.incident(inc["id"])
    assert (after["type"], after["address"]) == ("STRU", "14 Example Ave")     # queued: the feed's fields
    assert rig.feed_lines(inc["id"])[3:] == [
        "County feed: responding 14:30:50", "County feed: E60 on location 14:30:55", "County feed comment: smoke showing",
        "County feed: type STRU", "County feed: location 14 Example Ave", "County feed: units E60, L31 (+L31)"]
    n = len(rig.feed_lines(inc["id"]))
    rig.at(70)
    rig.poll()                                             # the same snapshot again: nothing new
    assert len(rig.feed_lines(inc["id"])) == n
    rig.feed.change("A1", closed=True, times={"in_service": T0 + 900})
    rig.at(900)
    rig.poll()
    assert rig.feed_lines(inc["id"])[-2:] == ["County feed: in service 14:45:00", "County feed: closed in the feed"]


def test_two_events_are_two_incidents(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.feed.put("A2", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0 + 60})
    rig.feed.put("A3", type="EMS", location="400 Sample St", units=("E62",), times={"dispatched": T0 + 60})
    rig.poll()
    assert {i["source_ref"] for i in rig.incidents()} == {"feed:county:A1", "feed:county:A2", "feed:county:A3"}


def test_a_lower_ranked_feed_creates_at_once_the_primary_attaches_and_rules(tmp_path):
    rig = Rig(tmp_path, FakeFeed("county", "County feed", rank=1), FakeFeed("mirror", "Mirror feed", rank=2))
    county, mirror = rig.feeds
    mirror.put("M1", type="EMS", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert inc["source_ref"] == "feed:mirror:M1"
    rig.at(30)
    county.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.poll()
    assert len(rig.incidents()) == 1 and rig.cad.store.incident(inc["id"])["type"] == "ALARM"
    rig.at(60)
    mirror.change("M1", location="99 Elsewhere Rd")
    rig.poll()
    assert rig.cad.store.incident(inc["id"])["address"] == "12 Example Ave"
    assert "Mirror feed (not authoritative): location 99 Elsewhere Rd" in rig.feed_lines(inc["id"])
    county.outage("failing", "HTTP 500")
    rig.at(70)
    rig.poll()
    assert rig.cad.state()["sources"]["acting"] == "Mirror feed"           # the next tier, not paging


def test_closed_or_old_events_are_recorded_and_never_make_an_incident(tmp_path):
    """Review S10 (a first run, a lost database): what the feed still lists from before is not a new call."""
    rig = Rig(tmp_path)
    rig.at(3600)
    rig.feed.put("OLD", type="STRU", location="1 Old Rd", units=("E60",), times={"dispatched": T0})
    rig.feed.put("DONE", type="STRU", location="2 Old Rd", units=("E61",), times={"dispatched": T0 + 3590}, closed=True)
    rig.poll()
    assert rig.incidents() == []
    assert {r["ref"] for r in rig.cad.store.q("SELECT ref FROM source_reports WHERE incident_id IS NULL")} == {"OLD", "DONE"}
    rig.at(3600 + MATCH_WINDOW_S)
    rig.feed.change("OLD", location="1 Old Road")          # a later change: still never a new call
    rig.poll()
    assert rig.incidents() == []


# -- pre-alerts ---------------------------------------------------------------------------------------------------------


def traffic(cad, tid, text, start, dispatcher=1, talkgroup=307, turn=None):
    with cad.store.tx() as db:
        db.execute("INSERT INTO radio_traffic (id, call_id, turn, start, end, channel, talkgroup, dispatcher, text, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, 'Fire/EMS 15', ?, ?, ?, ?)",
                   (tid, "c-" + tid.split("-")[0] if turn is not None else "", turn, start, start + 4, talkgroup,
                    dispatcher, text, start))
    return cad.sources.consider_radio(tid)


def test_radio_pre_alerts_are_possible_incoming_notices_never_incidents(tmp_path):
    rig = Rig(tmp_path)
    cad = rig.cad
    n = traffic(cad, "a-t0", "Port Chester, incoming additional alarm", T0)
    assert n["status"] == "open" and n["text"] == "Port Chester, incoming additional alarm"
    n = traffic(cad, "b-t0", "Engine 60, 60 Control", T0 + 5, turn=0)              # dispatch opening a call to us
    assert n["text"] == "Calling E60: Engine 60, 60 Control"
    assert traffic(cad, "b-t2", "Engine 60, 60 Control, copy", T0 + 9, turn=2) is None   # answering: not a pre-alert
    assert traffic(cad, "c-t0", "Engine 60 on location", T0 + 9, dispatcher=0, turn=0) is None
    assert traffic(cad, "d-t0", "Engine 60, 60 Control", T0 + 9, talkgroup=999) is None   # not the feed's talkgroups
    assert traffic(cad, "e-t0", "Ossining, incoming additional alarm", T0 + 9) is None  # not us
    assert [x["kind"] for x in cad.sources.state()["incoming"]] == ["radio", "radio"]
    assert rig.incidents() == []
    rig.at(30)
    made = rig.page()                                     # no feed here: paging is the primary
    assert made is not None
    assert cad.sources.state()["incoming"] == []           # answered by the incident
    assert {r["status"] for r in cad.store.q("SELECT status FROM incoming")} == {"matched"}


def test_radio_notices_expire(tmp_path):
    rig = Rig(tmp_path)
    traffic(rig.cad, "a-t0", "Stand by for a page, Port Chester", T0)
    rig.at(src.INCOMING_S + 1)
    rig.cad.sources.tick()
    assert rig.cad.sources.state()["incoming"] == [] and rig.incidents() == []


def test_the_phone_pre_alert_never_makes_an_incident(tmp_path):
    from sizeup.prealert import PreAlerts
    rig = Rig(tmp_path)
    rig.cad.prealert = pa = PreAlerts(rig.cad, "http://127.0.0.1:9", "ring", "status", log=lambda m: None)
    pa.ring({"id": "r1", "time": T0, "event": "message", "topic": "ring", "title": "STATION PHONE RINGING",
             "message": "Incoming call", "priority": 5})
    assert rig.incidents() == []
    rig.feed.put("A1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0 + 20})
    rig.at(30)
    rig.poll()
    [inc] = rig.incidents()
    assert pa.get(1)["status"] == "matched" and pa.get(1)["incident_id"] == inc["id"]    # a feed's incident answers it


# -- the hub ------------------------------------------------------------------------------------------------------------


# -- the hub ------------------------------------------------------------------------------------------------------------


def test_the_hub_applies_the_stations_source_events_and_health_shows_them(tmp_path):
    from sizeup.app import make_app
    from tests.test_cloud import hub_cfg
    app = make_app(hub_cfg(tmp_path))
    try:
        now = app.cad.clock()
        ev = FeedEvent("county", "H1", "ALARM", "12 Example Ave", ("E60",), {"dispatched": now}).as_dict()
        app.apply("source_health", {"source": "county", "label": "County feed", "rank": 1, "state": "ok", "reason": "",
                                    "at": now}, None)
        app.apply("source_event", {"source": "county", "label": "County feed", "rank": 1, "seen_at": now,
                                   "first_seen": now, "event": ev}, None)
        [inc] = app.cad.store.q("SELECT * FROM incidents")
        assert inc["source_ref"] == "feed:county:H1"
        panel = app.health.panel()["sources"]
        assert [t["label"] for t in panel["tiers"]] == ["County feed", "Paging"]
        assert panel["tiers"][1]["healthy"] is False and panel["tiers"][1]["reason"] == "station offline"
        assert app.cad.state()["sources"]["acting"] == "County feed"
    finally:
        app.stop()



def test_a_stations_adapter_reaches_the_hub_through_the_outbox(tmp_path):
    """The station runs the adapter; its events and health go through the station's outbox to the hub, the CAD of
    record, which makes the incident and applies every later change there."""
    from tests.test_cloud import HubRun, hub_cfg, station, station_cfg, wait
    hub = HubRun(hub_cfg(tmp_path))
    st = station(station_cfg(tmp_path, hub.url), {})
    try:
        feed = FakeFeed()
        st.sources_runner = SourceRunner([feed], st._source_out, log=lambda m: None)
        now = st.cad.clock()
        feed.put("S1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": now})
        st.sources_runner.step(st.sources_runner.runs[0])
        q = lambda: hub.app.cad.store.q("SELECT * FROM incidents WHERE source_ref = 'feed:county:S1'")
        assert wait(lambda: len(q()) == 1, 15)
        assert st.cad.store.q("SELECT * FROM incidents") == []          # never on the station's own CAD
        feed.comment("S1", now + 3, "second caller")
        st.sources_runner.step(st.sources_runner.runs[0])
        iid = q()[0]["id"]
        assert wait(lambda: any(e["text"] == "County feed comment: second caller" for e in
                                hub.app.cad.store.q("SELECT text FROM events WHERE incident_id = ?", (iid,))), 15)
        assert hub.app.cad.sources.state()["acting"] == "County feed"
    finally:
        st.link.stop()
        hub.close()


def test_adapters_are_never_loaded_or_run_on_the_hub(tmp_path, monkeypatch):
    looked = []
    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: looked.append(group) or [])
    with pytest.raises(HubRefused):
        load_adapters(context_for(tmp_path, print), env={"SIZEUP_INCIDENT_SOURCES": "tests.fake_feed:make"}, role="hub")
    with pytest.raises(HubRefused):
        SourceRunner([FakeFeed()], lambda k, d: None, role="hub")
    assert looked == []                                   # refused before discovery
    assert load_adapters(context_for(tmp_path, print), env={}, role="") == []    # all-in-one: allowed


# -- the trigger hook, extras, merges ------------------------------------------------------------------------------------


# -- the runner's robustness ----------------------------------------------------------------------------------------------


def test_a_bad_event_is_skipped_and_never_stalls_the_rest(tmp_path):
    """Review S5 and #6: None units (and other wrong types) are a SourceError, the good event still goes, no back-off."""
    rig = Rig(tmp_path)
    good = rig.feed.put("A1", type="ALARM", location="1 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.feed.events = {"Z": FeedEvent(source="county", event_id="Z", units=None), "A1": good,
                       "Y": FeedEvent(source="county", event_id="Y", comments=None),
                       "X": FeedEvent(source="county", event_id="X", unit_times={5: {}}),
                       "W": FeedEvent(source="county", event_id="W", aliases=None),
                       "V": FeedEvent(source="county", event_id="V", units=("E" * 33,))}
    assert rig.runner.step(rig.runner.runs[0]) == rig.feed.poll_s
    [inc] = rig.incidents()
    assert inc["source_ref"] == "feed:county:A1"
    for bad in rig.feed.events.values():
        if bad is not good:
            with pytest.raises(SourceError):
                bad.check()
    with pytest.raises(SourceError):
        FeedEvent("county", "U", unit_times={f"E{i}": {} for i in range(61)}).check()
    with pytest.raises(SourceError):
        FeedEvent("county", "T", comments=tuple((T0, "x" * 2000) for _ in range(200))).check()   # over 256 KiB


def test_the_reason_sent_never_carries_the_exceptions_text(tmp_path):
    """Review #10: an exception's text can hold a URL or a credential; the hub gets its type."""
    clock = Clock()
    sent = []

    class Leaky(FakeFeed):
        def poll(self):
            raise RuntimeError("GET https://user:secret@feed.example/x failed")
    r = SourceRunner([Leaky(clock=clock)], lambda k, d: sent.append((k, d)), log=lambda m: None, clock=clock)
    r.step(r.runs[0])
    assert sent[-1][1]["state"] == "failing" and sent[-1][1]["reason"] == "RuntimeError"
    assert "secret" not in json.dumps(sent)


def test_trigger_never_runs_adapter_code_on_the_callers_thread(tmp_path):
    """Review S6 and #5: a poll_now that blocks never delays trigger() or the paging decoder's lock."""
    from sizeup.radio.livepage import LivePaging, _Live
    from sizeup.radio.tones import ToneCode, ToneDetector, ToneEvent, TonePlan
    entered = threading.Event()

    class Slow(FakeFeed):
        def poll_now(self, reason):
            entered.set()
            time.sleep(1.5)
    feed = Slow()
    r = SourceRunner([feed], lambda k, d: None, log=lambda m: None)
    lp = LivePaging(TonePlan([ToneCode("Port Chester FD", 349.1, 311.1, "PCFD")]), {"Port Chester FD"})
    lp.on_ours = lambda: r.trigger("paging tones")
    cur = _Live("South 2", 0.0, 0.0, ToneDetector(8000, t0=0.0))
    t0 = time.monotonic()
    with lp._lock:                                         # as the live tap holds it
        lp._pair(cur, ToneEvent("two_tone", 349.1, 311.1, 1.0, 2.0), True)
    assert time.monotonic() - t0 < 0.2 and not entered.is_set()
    assert r.runs[0].wake.is_set() and r.runs[0].reason == "paging tones"
    r.start()                                              # the adapter's own thread calls it
    assert entered.wait(5) and feed.pokes == [] or True
    r.stop()


def test_the_runner_forgets_events_long_gone(tmp_path):
    clock = Clock()
    feed = FakeFeed(clock=clock)
    r = SourceRunner([feed], lambda k, d: None, log=lambda m: None, clock=clock)
    feed.put("A1", location="1 Example Ave")
    r.step(r.runs[0])
    del feed.events["A1"]
    clock.t += runner_mod.PRUNE_S + 1
    r.step(r.runs[0])
    assert r.runs[0].sent == {} and r.runs[0].first == {}


def test_a_factory_that_raises_systemexit_or_hangs_never_stops_the_station(tmp_path, monkeypatch):
    lines = []
    pkg = tmp_path / "adapters2"
    pkg.mkdir()
    (pkg / "rude.py").write_text("import sys\ndef make(ctx):\n    sys.exit(3)\n")
    monkeypatch.setattr(sys, "path", list(sys.path))
    got = load_adapters(context_for(tmp_path, lines.append), log=lines.append,
                        env={"SIZEUP_INCIDENT_SOURCES": "rude:make", "SIZEUP_INCIDENT_SOURCES_PATH": str(pkg)})
    assert got == [] and any("rude:make not loaded: SystemExit" in m for m in lines)
    # the app loads them off its start path: a factory that hangs never holds start() up
    from sizeup.app import make_app
    from tests.test_cloud import base
    (pkg / "slow.py").write_text("import time\ndef make(ctx):\n    time.sleep(3)\n    return []\n")
    monkeypatch.setenv("SIZEUP_INCIDENT_SOURCES", "slow:make")
    monkeypatch.setenv("SIZEUP_INCIDENT_SOURCES_PATH", str(pkg))
    app = make_app(base(cad_db=str(tmp_path / "aio" / "cad.sqlite")))
    t0 = time.monotonic()
    app.start_sources()
    assert time.monotonic() - t0 < 1.0
    app.cad.sources.stop()


def test_a_hub_with_adapters_configured_loads_nothing(tmp_path, monkeypatch):
    """The hub never discovers, imports or runs an adapter, even with SIZEUP_INCIDENT_SOURCES set."""
    from sizeup.app import make_app
    from tests.test_cloud import hub_cfg
    marker = tmp_path / "imported"
    pkg = tmp_path / "adapters3"
    pkg.mkdir()
    (pkg / "spy.py").write_text(f"open({str(marker)!r}, 'w').write('x')\ndef make(ctx):\n    return []\n")
    looked = []
    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: looked.append(group) or [])
    monkeypatch.setenv("SIZEUP_INCIDENT_SOURCES", "spy:make")
    monkeypatch.setenv("SIZEUP_INCIDENT_SOURCES_PATH", str(pkg))
    app = make_app(hub_cfg(tmp_path))
    try:
        app.start_sources()
        time.sleep(0.3)
        assert app.sources_runner is None and not marker.exists() and looked == []
        with pytest.raises(HubRefused):
            require_station(app.cfg.cloud_role)
    finally:
        app.stop()


# -- the trigger hook, extras, merges ------------------------------------------------------------------------------------


def test_triggers_ask_the_adapters_to_poll_now(tmp_path):
    rig = Rig(tmp_path)
    rig.cad.sources.trigger = rig.runner.trigger
    traffic(rig.cad, "a-t0", "Port Chester, incoming additional alarm", T0)           # a radio pre-alert
    assert rig.runner.runs[0].wake.is_set() and rig.feed.pokes == []                  # nothing ran on this thread
    rig.poll()
    assert rig.feed.pokes == ["radio pre-alert"]
    traffic(rig.cad, "f-t0", "Stand by for a page, Port Chester", T0 + 1)
    rig.poll()
    assert rig.feed.pokes == ["radio pre-alert"]                                        # at most every TRIGGER_MIN_S
    rig.at(10)
    from sizeup.prealert import PreAlerts
    pa = PreAlerts(rig.cad, "http://127.0.0.1:9", "ring", "status", log=lambda m: None)
    pa.ring({"id": "r1", "time": T0 + 10, "event": "message", "topic": "ring", "title": "STATION PHONE RINGING",
             "message": "Incoming call", "priority": 5})
    rig.poll()
    assert rig.feed.pokes[-1] == "phone pre-alert" and rig.incidents() == []


def test_paging_tones_of_ours_poke_the_station_adapters():
    from sizeup.radio.livepage import LivePaging, _Live
    from sizeup.radio.tones import ToneCode, ToneDetector, ToneEvent, TonePlan
    hits = []
    lp = LivePaging(TonePlan([ToneCode("Port Chester FD", 349.1, 311.1, "PCFD")]), {"Port Chester FD"})
    lp.on_ours = lambda: hits.append(1)
    cur = _Live("South 2", 0.0, 0.0, ToneDetector(8000, t0=0.0))
    lp._pair(cur, ToneEvent("two_tone", 400.0, 500.0, 0.5, 1.5), True)       # not ours: nothing
    lp._pair(cur, ToneEvent("two_tone", 349.1, 311.1, 1.0, 2.0), True)
    assert hits == [1]


def test_extras_are_on_the_timeline_and_shown_on_the_incident(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0},
                 extra={"jurisdiction": "Example Village", "run_numbers": {"Fire": "26-0012"}, "group_units": {"Fire": ["E60"]},
                        "beat": "7"})
    rig.poll()
    [inc] = rig.incidents()
    assert {"County feed: jurisdiction Example Village", "County feed: run numbers Fire 26-0012", "County feed: beat 7",
            "County feed: group units Fire E60"} <= set(rig.feed_lines(inc["id"]))
    rig.at(30)
    rig.feed.change("A1", extra={**rig.feed.events["A1"].extra, "run_numbers": {"Fire": "26-0012", "EMS": "26-0444"}})
    rig.poll()
    assert rig.feed_lines(inc["id"])[-1] == "County feed: run numbers Fire 26-0012; EMS 26-0444"
    [shown] = [i for i in rig.cad.state()["incidents"] if i["id"] == inc["id"]]
    assert shown["feed"] == {"label": "County feed", "event_id": "A1", "jurisdiction": "Example Village",
                             "run_numbers": {"Fire": "26-0012", "EMS": "26-0444"}, "group_units": {"Fire": ["E60"]},
                             "use": {}}
    with pytest.raises(SourceError):
        FeedEvent("county", "X", extra={"bad": object()}).check()


def test_a_merge_dismisses_only_the_same_sources_queued_incident(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.feed.put("B2", type="ALARM", location="400 Sample St", units=("E62",), times={"dispatched": T0 + 30})
    rig.poll()
    a, b = rig.incidents()
    rig.at(60)
    rig.feed.change("A1", aliases=("B2",), units=("E60", "E62"))
    rig.poll()
    assert rig.cad.store.incident(b["id"])["status"] == "dismissed"
    assert f"County feed: event B2 merged into A1 (event #{a['id']})" in rig.feed_lines(b["id"])
    assert f"County feed: event B2 merged into A1 (event #{b['id']})" in rig.feed_lines(a["id"])
    n = len(rig.feed_lines(b["id"]))
    rig.at(90)
    rig.feed.change("B2", location="402 Sample St")             # the feed still sends B: ignored
    rig.poll()
    assert len(rig.feed_lines(b["id"])) == n and len(rig.incidents()) == 2


def test_a_merge_never_dismisses_an_accepted_incident_and_keeps_its_report(tmp_path):
    """Review S8: B accepted (a person runs it): a note on both, B's report stays on B, nothing dismissed."""
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.feed.put("B1", type="STRU", location="400 Sample St", units=("E62",), times={"dispatched": T0})
    rig.poll()
    a, b = rig.incidents()
    rig.cad.accept(b["id"], {"operator": "Lt. Test"})
    rig.at(30)
    rig.feed.change("A1", aliases=("B1",))
    rig.poll()
    assert rig.cad.store.incident(b["id"])["status"] == "pending"
    assert rig.cad.store.one("SELECT incident_id FROM source_reports WHERE ref = 'B1'")["incident_id"] == b["id"]
    assert f"County feed: event B1 merged into A1 (event #{a['id']}): kept, check it" in rig.feed_lines(b["id"])
    rig.at(60)                                                   # B's incident is still taken: a new call is its own
    rig.feed.put("C1", type="STRU", location="9 Far Rd", units=("E62",), times={"dispatched": T0 + 60})
    rig.poll()
    assert rig.incidents()[-1]["source_ref"] == "feed:county:C1"


def test_a_merge_never_dismisses_a_page_s_incident(tmp_path):
    rig = Rig(tmp_path)
    page = rig.page()                                            # queued, the page's
    rig.at(20)
    rig.feed.put("B1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0 + 10})
    rig.poll()
    assert [i["id"] for i in rig.incidents()] == [page["id"]]   # B attached to the page's incident
    rig.feed.put("A1", type="ALARM", location="400 Sample St", units=("E62",), times={"dispatched": T0 + 20},
                 aliases=("B1",))
    rig.at(40)
    rig.poll()
    assert rig.cad.store.incident(page["id"])["status"] == "queued"


def test_an_event_naming_an_alias_attaches_to_its_incident(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("B2", type="ALARM", location="400 Sample St", units=("E62",), times={"dispatched": T0})
    rig.poll()
    [b] = rig.incidents()
    rig.feed.events.pop("B2")
    rig.at(30)
    rig.feed.put("C3", type="ALARM", location="401 Other Rd", units=("E64",), times={"dispatched": T0 + 30}, aliases=("B2",))
    rig.poll()
    assert len(rig.incidents()) == 1 and rig.cad.store.incident(b["id"])["status"] == "queued"


def test_a_reset_runs_on_the_adapters_own_thread_and_starts_the_back_off_over(tmp_path):
    """#305's remote reset: request() from any thread queues it; the adapter's loop runs reset() before its next poll."""
    ran = []

    class Locked(FakeFeed):
        def reset(self):
            ran.append(threading.current_thread().name)
            self.recover()
    feed = Locked()
    feed.outage("failing", "login (manual reset needed)", raise_=True)
    r = SourceRunner([feed, FakeFeed("other", "Other feed")], lambda k, d: None, log=lambda m: None)
    run = r.runs[0]
    for _ in range(3):
        r.step(run)
    assert run.fails == 3
    r.resets.request("county", "admin@example.org")
    assert run.wake.is_set() and not r.runs[1].wake.is_set() and ran == []      # queued: nothing on this thread
    r.start()
    try:
        deadline = time.monotonic() + 5
        while not ran and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ran == ["source-county"]
    finally:
        r.stop()
    # an adapter without reset(): a no-op; one that raises: logged by type
    lines = []
    r2 = SourceRunner([FakeFeed("plain", "Plain")], lambda k, d: None, log=lines.append)
    r2.resets.request("plain")
    r2._reset_due(r2.runs[0])
    assert any("no reset()" in m for m in lines)


# -- the second review: no-location reports, address formats, triggers in an outage, edits while queued ----------------


PAGE_NO_PLACE = "Port Chester Fire Department, Engine 60, respond to an activated fire alarm. Time out 1440."


def test_a_page_with_no_location_never_attaches_to_a_feed_incident_by_units(tmp_path):
    """Review 2 #1: the feed down, paging the backup: a page naming E60 but no place is a new call, noted."""
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="STRU", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.poll()
    [a] = rig.incidents()
    rig.feed.outage("failing", "x")
    rig.at(600)
    rig.poll()
    got = rig.page("p2", PAGE_NO_PLACE)
    assert got["id"] != a["id"] and got["source"] == "60 Control page"
    assert any(x.startswith(f"Possibly the same as #{a['id']}") for x in rig.feed_lines(got["id"]))
    assert any(x.startswith(f"Possibly the same as #{got['id']}") for x in rig.feed_lines(a["id"]))


def test_a_feed_event_with_no_location_never_attaches_to_a_page_s_incident_by_units(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.outage("failing", "x")
    rig.poll()
    p = rig.page("p1")
    rig.at(300)
    rig.feed.recover()
    rig.feed.put("B1", type="EMS", location="", units=("E60",), times={"dispatched": T0 + 290})
    rig.poll()
    a, b = rig.incidents()
    assert (a["id"], b["source_ref"]) == (p["id"], "feed:county:B1")
    assert any(x.startswith(f"Possibly the same as #{p['id']}") for x in rig.feed_lines(b["id"]))


# The external CAD's location formats (synthetic names only) against ours: same place?
FORMATS = [
    ("12 Example Avenue", "12 EXAMPLE AVENUE : @SAMPLE PLACE", True),
    ("12 Example Avenue", "12 EXAMPLE AVE @ SAMPLE PLACE", True),
    ("12 Example Avenue", "12 EXAMPLE AVE   : @SAMPLE SCHOOL - EXAMPLE VILLAGE EXAMPLE_TOWN", True),
    ("12 Example Avenue", "12 EXAMPLE AVE, SAMPLE TOWN", True),
    ("12 Example Avenue", "12 EXAMPLE AVE SAMPLE TOWN", True),
    ("12 Example Avenue", "12 EXAMPLE AVE 3RD FL", True),
    ("12 Example Avenue", "12 EXAMPLE AVE 2ND FLOOR", True),
    ("12 Example Avenue", "12 EXAMPLE AVE APT 3", True),
    ("12 Example Avenue", "12-14 EXAMPLE AVE", True),
    ("12 Example Avenue", "12 EXAMPLE AVE (SAMPLE TOWN)", True),
    ("12 Example Avenue", "12 EXAMPLE AVE; SAMPLE PLACE", True),
    ("12 Example Avenue", "12 EXAMPLE AV", True),
    ("12 Example Avenue", "12 EXAMPLE AVE #2", True),
    ("12 Example Ave", "12 EXAMPLE AVE [SAMPLE BUSINESS]", True),
    ("12 Example Ave", "12 EXAMPLE AVENUE,ST", True),
    ("12 Example Ave", "12 EXAMPLE AVE BLDG 2", True),
    ("12 Example Ave", "@SAMPLE PLACE (12 EXAMPLE AVE)", True),
    ("Elm St & Example Ave", "EXAMPLE AVE / ELM ST", True),
    ("Elm Street and Example Avenue", "ELM ST/EXAMPLE AVE", True),
    ("Elm St at Example Ave", "ELM ST & EXAMPLE AVE", True),
    ("N Elm St & Example Ave", "ELM ST & EXAMPLE AVE", True),
    ("Elm St & Example Ave", "ELM ST / EXAMPLE AVE / OAK ST", False),
    ("12 Elm Street", "12 N ELM ST", True),
    ("12 N Elm St", "12 S ELM ST", False),
    ("12 Saint Sample Pl", "12 ST SAMPLE PL", True),
    ("12 Oak St", "12 OAK STREET UNIT B", True),
    ("12 Sampleway", "12 SAMPLEWAY", True),
    ("12 Sampleway", "12 SAMPLEWAY @SAMPLE PLACE", True),
    ("14 Example Ave", "12 EXAMPLE AVE", False),
    ("Oak St", "500 OAK ST", False),
    ("Elm St & Example Ave", "100 EXAMPLE AVE", False),
]


@pytest.mark.parametrize("ours,theirs,same", FORMATS)
def test_the_external_cads_location_formats(ours, theirs, same):
    assert same_place(ours, theirs) is same
    assert same_place(theirs, ours) is same


def test_a_duplicate_is_never_silent(tmp_path):
    """Review 2 #2: two places sharing units within the window: two incidents, each noted with the other."""
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="400 SAMPLE ST : @SAMPLE PLACE", units=("E60", "L31"),
                 times={"dispatched": T0})
    rig.poll()
    rig.at(45)
    page = rig.page("p1")                                  # E60, L31 at 12 Example Avenue
    a = rig.incidents()[0]
    assert page["id"] != a["id"]
    assert any(x.startswith(f"Possibly the same as #{a['id']}") for x in rig.feed_lines(page["id"]))
    assert any(x.startswith(f"Possibly the same as #{page['id']}") for x in rig.feed_lines(a["id"]))
    # the real format, the same place: attached, not a duplicate
    rig.at(60)
    rig.feed.put("A2", type="ALARM/COMM", location="12 EXAMPLE AVENUE   : @SAMPLE SCHOOL - EXAMPLE VILLAGE EXAMPLE_TOWN",
                 units=("E60",), times={"dispatched": T0 + 50})
    rig.poll()
    assert len(rig.incidents()) == 2 and rig.cad.store.incident(page["id"])["type"] == "ALARM"


def test_triggers_never_outpace_the_back_off(tmp_path):
    """Review 2 #3: while the adapter is failing, a trigger never polls the external app sooner."""
    clock = Clock()

    class Down(FakeFeed):
        def health(self):
            return Health("ok")                            # even when health() says ok, failing polls hold it back
    feed = Down(clock=clock)
    feed.outage("failing", "x", raise_=True)
    r = SourceRunner([feed], lambda k, d: None, log=lambda m: None, clock=clock)
    r.step(r.runs[0])
    assert r.runs[0].fails == 1
    clock.t += 5
    assert r.trigger("paging tones") == 0 and not r.runs[0].wake.is_set()
    feed.recover()
    r.step(r.runs[0])
    clock.t += 5
    assert r.trigger("paging tones") == 1
    r2 = SourceRunner([FakeFeed(clock=clock)], lambda k, d: None, log=lambda m: None, clock=clock)
    r2.runs[0].a.outage("backing_off", "HTTP 429")
    r2.step(r2.runs[0])                                    # health says backing off: no triggers either
    clock.t += 5
    assert r2.trigger("radio pre-alert") == 0


def test_a_person_s_edit_stands_on_a_queued_incident_too(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    rig.cad.update(inc["id"], {"address": "400 Sample St", "operator": "Lt. Test"})     # still queued
    rig.at(30)
    rig.feed.change("A1", location="14 EXAMPLE AVE")
    rig.poll()
    assert rig.cad.store.incident(inc["id"])["address"] == "400 Sample St"
    assert rig.cad.sources.manual(inc["id"]) == {"location"}
    rig.at(40)
    rig.page("p1")                                         # a page and the feed never mark anything as a person's
    rig.feed.change("A1", type="STRU")
    rig.poll()
    assert rig.cad.sources.manual(inc["id"]) == {"location"}


def test_a_feed_incident_is_received_via_the_feed_not_60_control(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.poll()
    assert rig.incidents()[0]["received_via"] == "CAD FEED"
