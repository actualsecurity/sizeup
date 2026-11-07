"""Incident sources, ranked (#297): the adapter interface (sizeup/sources/), the station's runner, and the CAD's rules
(sizeup/cad/sources.py): promotion, demotion, holding, reconciliation, no duplicates, the timeline, the lead over
paging, and pre-alerts that never make an incident. Synthetic data only: FakeFeed (tests/fake_feed.py) with made-up
events, places and times."""

from __future__ import annotations

import sys
import types

import pytest

from sizeup.cad import sources as src
from sizeup.cad.sources import HOLD_S, PAGING, REPORT_STALE_S, norm_unit, same_place, units_match
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore
from sizeup.sources import FeedEvent, Health, SourceError
from sizeup.sources.runner import HEARTBEAT_S, HubRefused, SourceRunner, check_adapter, context_for, load_adapters
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
        for r in self.runner.runs:
            self.runner.step(r)

    def at(self, dt: float) -> None:
        self.clock.t = T0 + dt

    def page(self, pid: str = "p1", text: str = PAGE):
        inc = self.monitor.on_page(pid, ["Port Chester FD"], parse(text), at=self.clock.t)
        return self.cad.from_monitor(inc)

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
    assert any("my_adapter:broken not loaded: RuntimeError: no credentials" in m for m in lines)
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


def test_matching_helpers():
    assert norm_unit("Engine 60") == norm_unit("E-60") == "E60" and norm_unit("Tower Ladder 2") == "TL2"
    assert units_match(["Engine 60", "Ladder 31"], ["E60"]) == {"E60"} and not units_match(["E61"], ["E60"])
    assert same_place("12 EXAMPLE AVE", "12 Example Avenue") and not same_place("14 Example Ave", "12 Example Avenue")
    assert same_place("Example Ave", "12 Example Avenue") and not same_place("12 Sample St", "12 Example Ave")


def test_the_primary_feed_creates_and_the_page_attaches_without_a_duplicate(tmp_path):
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


def test_a_page_is_held_while_the_primary_is_healthy_and_attached_when_the_feed_reports_it(tmp_path):
    rig = Rig(tmp_path)
    rig.poll()                                             # the feed is up (health ok), with nothing yet
    assert rig.page() is None and rig.incidents() == []    # held: the healthy feed may still report it
    [held] = rig.cad.sources.state()["incoming"]
    assert (held["kind"], held["source"], held["until"]) == ("held", "Paging", T0 + HOLD_S)
    assert held["text"] == "Fire alarm; 12 Example Avenue; Engine 60, Ladder 31"
    rig.at(20)
    rig.feed.put("A1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0 - 5})
    rig.poll()
    [inc] = rig.incidents()
    assert inc["source"] == "CAD feed"
    ref = rig.cad.store.one("SELECT ref FROM source_reports WHERE source = 'paging'")["ref"]
    assert rig.cad.sources.paging_incident(ref) == inc["id"]
    assert rig.cad.sources.state()["incoming"] == []
    rig.at(HOLD_S + 30)
    rig.cad.sources.tick()
    assert len(rig.incidents()) == 1                       # never a second one at the end of the hold
    [row] = rig.cad.sources.panel()["incidents"]
    assert {s["source"]: s["lead_s"] for s in row["seen"]} == {PAGING: None, "county": -20.0}   # paging was first


def test_a_held_page_creates_the_incident_when_the_feed_never_reports_it(tmp_path):
    rig = Rig(tmp_path)
    rig.poll()
    assert rig.page() is None
    rig.at(HOLD_S - 1)
    rig.cad.sources.tick()
    assert rig.incidents() == []
    rig.at(HOLD_S)
    rig.cad.sources.tick()
    [inc] = rig.incidents()
    assert (inc["source"], inc["address"]) == ("60 Control page", "12 Example Avenue")
    rig.at(HOLD_S + 30)                                    # the feed catches up: attached, authoritative
    rig.feed.put("A1", type="STRU", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.poll()
    assert len(rig.incidents()) == 1
    after = rig.cad.store.incident(inc["id"])
    assert (after["type"], after["address"]) == ("STRU", "12 Example Avenue")    # the same place: ours stays (its pin)
    lines = rig.feed_lines(inc["id"])
    [att] = [x for x in lines if x.startswith("County feed: attached (")]
    assert att == "County feed: attached (1:30 after Paging) · now authoritative"
    assert "County feed: type STRU" in lines


def test_promotion_when_the_primary_fails_and_demotion_when_it_recovers(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.outage("backing_off", "HTTP 429")
    rig.poll()
    st = rig.cad.sources.state()
    assert st["acting"] == "Paging"
    assert [(t["source"], t["state"], t["acting"]) for t in st["tiers"]] == [("county", "backing_off", False), (PAGING, "ok", True)]
    got = rig.page()                                       # paging is acting primary: it creates at once
    assert got is not None and got["source"] == "60 Control page"
    rig.feed.recover()
    rig.at(30)
    rig.poll()
    assert rig.cad.sources.state()["acting"] == "County feed"
    rig.monitor = IncidentStore({"Port Chester FD"}, geocode=lambda d: None)     # another call
    assert rig.page("p9", OTHER_PAGE) is None              # demoted again: the next page waits for the feed
    assert len(rig.incidents()) == 1


def test_a_feed_gone_stale_is_not_healthy_and_what_was_held_goes_at_once(tmp_path):
    rig = Rig(tmp_path)
    rig.poll()
    rig.at(10)
    assert rig.page() is None
    rig.at(REPORT_STALE_S + 1)                             # no report from the station since T0
    t = rig.cad.sources.tiers()[0]
    assert (t["state"], t["healthy"]) == ("stale", False) and t["reason"].startswith("no report since")
    rig.cad.sources.tick()
    [inc] = rig.incidents()
    assert inc["source"] == "60 Control page"


def test_the_held_page_goes_as_soon_as_the_feed_fails(tmp_path):
    rig = Rig(tmp_path)
    rig.poll()
    assert rig.page() is None
    rig.at(5)
    rig.feed.outage("failing", "connection refused", raise_=True)
    rig.poll()                                             # the health report itself promotes paging
    [inc] = rig.incidents()
    assert inc["source"] == "60 Control page"


def test_paging_attaches_to_a_manual_incident_and_a_station_offline_takes_paging_out(tmp_path):
    rig = Rig(tmp_path)
    rig.cad.sources.paging_ok = lambda: False
    assert rig.cad.sources.acting() is None
    rig.cad.sources.paging_ok = lambda: True
    made = rig.cad.create({"type": "ALARM", "subtype": "COMM", "address": "12 Example Avenue", "operator": "Lt. Test"})
    rig.at(40)
    assert rig.page()["id"] == made["id"] and len(rig.incidents()) == 1


def test_every_feed_change_and_comment_is_on_the_timeline_and_its_fields_rule(tmp_path):
    rig = Rig(tmp_path)
    ev = rig.feed.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0},
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
    assert (after["type"], after["address"]) == ("STRU", "14 Example Ave")
    lines = rig.feed_lines(inc["id"])[3:]
    assert lines == ["County feed: responding 14:30:50", "County feed: E60 on location 14:30:55",
                     "County feed comment: smoke showing", "County feed: type STRU", "County feed: location 14 Example Ave",
                     "County feed: units E60, L31 (+L31)"]
    n = len(rig.feed_lines(inc["id"]))
    rig.at(70)
    rig.poll()                                             # the same snapshot again: nothing new
    assert len(rig.feed_lines(inc["id"])) == n
    rig.feed.change("A1", closed=True, times={"in_service": T0 + 900})
    rig.at(900)
    rig.poll()
    assert rig.feed_lines(inc["id"])[-2:] == ["County feed: in service 14:45:00", "County feed: closed in the feed"]
    assert ev.event_id == "A1" and len(rig.incidents()) == 1


def test_two_events_are_two_incidents_and_a_source_never_attaches_twice_to_one(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.feed.put("A2", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0 + 60})
    rig.feed.put("A3", type="EMS", location="400 Sample St", units=("E62",), times={"dispatched": T0 + 60})
    rig.poll()
    incs = rig.incidents()
    assert len(incs) == 3 and {i["source_ref"] for i in incs} == {"feed:county:A1", "feed:county:A2", "feed:county:A3"}


def test_a_lower_ranked_feed_is_held_then_attached_and_never_authoritative(tmp_path):
    rig = Rig(tmp_path, FakeFeed("county", "County feed", rank=1), FakeFeed("mirror", "Mirror feed", rank=2))
    county, mirror = rig.feeds
    rig.poll()
    mirror.put("M1", type="EMS", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.poll()
    assert rig.incidents() == [] and rig.cad.sources.state()["incoming"][0]["source"] == "Mirror feed"
    rig.at(30)
    county.put("A1", type="ALARM/COMM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert inc["source_ref"] == "feed:county:A1"
    rig.at(60)
    mirror.change("M1", location="99 Elsewhere Rd")
    rig.poll()
    assert rig.cad.store.incident(inc["id"])["address"] == "12 Example Ave"     # the mirror only notes it
    assert "Mirror feed (not authoritative): location 99 Elsewhere Rd" in rig.feed_lines(inc["id"])
    county.outage("failing", "HTTP 500")
    rig.at(70)
    rig.poll()
    assert rig.cad.sources.state()["acting"] == "Mirror feed"                    # the next tier, not paging


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


def test_triggers_ask_the_adapters_to_poll_now(tmp_path):
    rig = Rig(tmp_path)
    rig.cad.sources.trigger = rig.runner.trigger
    traffic(rig.cad, "a-t0", "Port Chester, incoming additional alarm", T0)           # a radio pre-alert
    assert rig.feed.pokes == ["radio pre-alert"] and rig.runner.runs[0].wake.is_set()
    traffic(rig.cad, "f-t0", "Stand by for a page, Port Chester", T0 + 1)
    assert rig.feed.pokes == ["radio pre-alert"]                                        # at most every TRIGGER_MIN_S
    rig.at(10)
    from sizeup.prealert import PreAlerts
    pa = PreAlerts(rig.cad, "http://127.0.0.1:9", "ring", "status", log=lambda m: None)
    pa.ring({"id": "r1", "time": T0 + 10, "event": "message", "topic": "ring", "title": "STATION PHONE RINGING",
             "message": "Incoming call", "priority": 5})
    assert rig.feed.pokes[-1] == "phone pre-alert" and rig.incidents() == []


def test_paging_tones_of_ours_poke_the_station_adapters():
    from sizeup.radio.livepage import LivePaging
    from sizeup.radio.tones import TonePlan, ToneCode, ToneEvent
    hits = []
    lp = LivePaging(TonePlan([ToneCode("Port Chester FD", 349.1, 311.1, "PCFD")]), {"Port Chester FD"})
    lp.on_ours = lambda: hits.append(1)
    from sizeup.radio.livepage import _Live
    from sizeup.radio.tones import ToneDetector
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
                             "run_numbers": {"Fire": "26-0012", "EMS": "26-0444"}, "group_units": {"Fire": ["E60"]}}
    with pytest.raises(SourceError):
        FeedEvent("county", "X", extra={"bad": object()}).check()


def test_a_merge_retires_the_other_event_and_its_queued_incident(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM", location="12 Example Ave", units=("E60",), times={"dispatched": T0})
    rig.feed.put("B2", type="ALARM", location="400 Sample St", units=("E62",), times={"dispatched": T0 + 30})
    rig.poll()
    a, b = rig.incidents()
    rig.at(60)
    rig.feed.change("A1", aliases=("B2",), units=("E60", "E62"))
    rig.poll()
    b_now = rig.cad.store.incident(b["id"])
    assert b_now["status"] == "dismissed"
    assert f"County feed: event B2 merged into A1 (event #{a['id']})" in rig.feed_lines(b["id"])
    assert f"County feed: event B2 merged into A1 (event #{b['id']})" in rig.feed_lines(a["id"])
    n = len(rig.feed_lines(b["id"]))
    rig.at(90)
    rig.feed.change("B2", location="402 Sample St")             # the feed still sends B: ignored
    rig.poll()
    assert len(rig.feed_lines(b["id"])) == n and len(rig.incidents()) == 2
    assert [i["feed"]["event_id"] for i in rig.cad.state()["incidents"] if i["id"] == a["id"]] == ["A1"]


def test_an_event_naming_an_alias_attaches_to_its_incident(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("B2", type="ALARM", location="400 Sample St", units=("E62",), times={"dispatched": T0})
    rig.poll()
    [b] = rig.incidents()
    b_ev = rig.feed.events.pop("B2")
    rig.at(30)
    rig.feed.put("C3", type="ALARM", location="401 Other Rd", units=("E64",), times={"dispatched": T0 + 30}, aliases=("B2",))
    rig.poll()
    assert len(rig.incidents()) == 1 and rig.cad.store.incident(b["id"])["status"] == "queued"
    assert b_ev.event_id == "B2"
