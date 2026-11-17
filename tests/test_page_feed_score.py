"""A page against the county feed's incident it is (sizeup/cad/pagematch.py, Sources.page_feed): the page's house number
said twice and run together ("412412", sizeup/dispatch.py undouble), a scored join when the strict rules find none, and
the "Merge into #N" a weaker match offers on the page's own card. The scenario: the feed made the incident at a business
on a long road; a minute later the page said the number twice, the business, two cross streets and "inside odor of
gas", and the page made a second card ("Hazard / Gas at <street>", only "possibly the same").

Synthetic only: made-up streets, businesses, numbers and times (FakeFeed, tests/fake_feed.py)."""

from __future__ import annotations

import json

import pytest

from sizeup import dispatch
from sizeup.cad import pagematch
from sizeup.cad.api import CadApi, needs
from sizeup.cloud.access import Identity
from sizeup.dispatch import parse, undouble
from tests.test_dispatch_modes import AutoRig
from sizeup.cad.sources import SAME_CALL_S
from tests.test_incident_sources import Rig
from tests.test_street_only import feed_event, open_incidents, possibly

FEED_AT = "412 SAMPLE POST RD : @SAMPLE FITNESS"
PAGE = ("Port Chester Fire Department, full response, 412412 Sample Post Road, Sample Fitness in Example Avenue and "
        "Specimen Street. Inside odor of gas. Time out 1431.")
PAGE_UNITS = PAGE.replace("full response", "Engine 60, Ladder 31")
PLACES = [{"key": "cp:sample-fitness", "name": "SAMPLE FITNESS", "aliases": [], "address": "412 SAMPLE POST RD",
           "muni": "port chester", "label": "Sample Fitness"},
          {"key": "cp:specimen-diner", "name": "SPECIMEN DINER", "aliases": [], "address": "88 SAMPLE POST RD",
           "muni": "port chester", "label": "Specimen Diner"}]


def lost_number(monkeypatch):
    """The page as it was read before undouble: "412412" no house number, the location only the street."""
    monkeypatch.setattr(dispatch, "undouble", lambda t: t)
    assert parse(PAGE).location == "sample post road"


def places(rig):
    rig.cad.local_places = lambda: [dict(p) for p in PLACES]


def feed_lines(rig, iid):
    return rig.feed_lines(iid)


def paging_on(rig) -> int | None:
    r = rig.cad.store.one("SELECT incident_id FROM source_reports WHERE source = 'paging'")
    return r["incident_id"] if r else None


def state_inc(rig, iid):
    return next(i for i in rig.cad.state()["incidents"] if i["id"] == iid)


# -- the normaliser: a house number said twice, run together ---------------------------------------------------------


@pytest.mark.parametrize("text, want", [
    ("412412 Sample Post Road", "412 Sample Post Road"),
    ("12341234 Example Avenue", "1234 Example Avenue"),
    ("at 412412, Sample Post Road", "at 412, Sample Post Road"),
    ("1717 Example Lane", "1717 Example Lane"),            # four digits may be a real number: matching halves it
    ("412413 Sample Post Road", "412413 Sample Post Road"),  # not a doubling
    ("time out 143143", "time out 143143"),                  # no word after it: never a house number
    ("Engine 6060 Example Lane", "Engine 6060 Example Lane"),
    ("012012 Example Lane", "012012 Example Lane"),
])
def test_undouble(text, want):
    assert undouble(text) == want


def test_the_page_keeps_its_house_number_and_cross_streets():
    d = parse(PAGE)
    assert d.location == "412 sample post road"
    assert d.cross_street == "example avenue and specimen street"
    assert d.incident_type == "Gas leak" and d.time_out == "14:31"
    assert parse(PAGE.replace("412412", "412 412")).location == "412 sample post road"   # the old repeat rule


def test_the_page_locator_reads_it_once(monkeypatch):
    from sizeup.geo import pagegeo
    seen = []

    class Loc:
        def locate(self, text, home):
            seen.append(text)
            return None
    monkeypatch.setattr(pagegeo, "locator", lambda gaz: Loc())
    pagegeo.locate_page(object(), PAGE, ["Port Chester FD"])
    assert "412 Sample Post Road" in seen[0] and "412412" not in seen[0]


# -- the scenario --------------------------------------------------------------------------------------------------


def test_the_scenario_joins_the_feeds_incident_now_the_number_is_read(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="INVEST/GAS-ODOR")
    rig.at(60)
    got = rig.page("p1", PAGE)
    assert got["id"] == a and [i["id"] for i in open_incidents(rig)] == [a]
    assert paging_on(rig) == a
    assert not any(x.startswith("Possibly the same") for x in feed_lines(rig, a))
    assert not any(" matched by: " in x for x in feed_lines(rig, a))           # the strict same-place rule, not the score


def test_the_scenario_with_the_number_lost_was_a_second_card_and_now_joins_on_the_score(tmp_path, monkeypatch):
    lost_number(monkeypatch)
    rig = Rig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="INVEST/GAS-ODOR")
    rig.at(60)
    got = rig.page("p1", PAGE)
    assert got["id"] == a and len(open_incidents(rig)) == 1                     # no second card
    inc = rig.cad.store.incident(a)
    assert (inc["type"], inc["subtype"]) == ("INVEST", "GAS-ODOR")              # the feed's type stands
    assert inc["address"].startswith("412 Sample Post Rd")                       # ... and its location
    line = next(x for x in feed_lines(rig, a) if " matched by: " in x)
    assert "street" in line and "the only open county call" in line and "place Sample Fitness" in line
    assert "type family" in line
    tones = [e["text"] for e in rig.cad.store.q("SELECT text FROM events WHERE incident_id = ? AND kind = 'tones'", (a,))]
    assert any("Sample Fitness" in x for x in tones)                             # its transcript is on it


def test_a_digit_more_on_an_exactly_right_street_is_another_house(tmp_path):
    """"4120" for 412 on the street as typed: another house (the "misheard" rule's review), never joined or offered."""
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="HAZARD/GAS")
    rig.at(90)
    p = rig.page("p1", PAGE.replace("412412", "4120").replace(", Sample Fitness", ""))["id"]
    assert p != a and "merge_offer" not in state_inc(rig, p)


def test_the_page_first_then_the_feed_sweeps_its_card_on_the_score(tmp_path, monkeypatch):
    lost_number(monkeypatch)
    rig = Rig(tmp_path)
    places(rig)
    rig.poll()                                                                   # the feed up, nothing listed yet
    p = rig.page("p1", PAGE)["id"]
    assert rig.cad.store.incident(p)["status"] == "queued"
    rig.at(40)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="INVEST/GAS-ODOR", dt=-20)
    assert a != p and rig.cad.store.incident(p)["status"] == "dismissed"
    assert paging_on(rig) == a and [i["id"] for i in open_incidents(rig)] == [a]
    assert any(" matched by: " in x for x in feed_lines(rig, a))


# -- never two different calls ---------------------------------------------------------------------------------------


def test_same_street_another_business_and_a_far_number_never_joins_nor_offers(tmp_path):
    rig = Rig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="HAZARD/GAS")
    rig.at(60)
    other = PAGE.replace("412412 Sample Post Road, Sample Fitness", "88 Sample Post Road, Specimen Diner")
    p = rig.page("p1", other.replace("Example Avenue and Specimen Street", "Model Street and Pattern Lane"))["id"]
    assert p != a and len(open_incidents(rig)) == 2 and paging_on(rig) == p
    assert "merge_offer" not in state_inc(rig, p)


def test_another_business_named_on_the_street_is_never_strong(tmp_path, monkeypatch):
    lost_number(monkeypatch)
    rig = Rig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="HAZARD/GAS")
    rig.at(60)
    p = rig.page("p1", PAGE.replace("Sample Fitness", "Specimen Diner"))["id"]
    assert p != a
    offer = state_inc(rig, p)["merge_offer"]                                     # a person decides
    assert offer["into"] == a and "not joined: names Specimen Diner" in offer["why"]


def test_two_open_county_calls_in_the_window_never_auto_join(tmp_path, monkeypatch):
    """(The feed's type another than the page's, as in the scenario: the strict street-only rule never joins it.)"""
    lost_number(monkeypatch)
    rig = Rig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="INVEST/GAS-ODOR")
    b = feed_event(rig, "A2", "9 MODEL ST", ("PCFD",), typ="ALARM/RESD", dt=30)
    rig.at(90)
    p = rig.page("p1", PAGE)["id"]
    assert p not in (a, b) and len(open_incidents(rig)) == 3
    offer = state_inc(rig, p)["merge_offer"]
    assert offer["into"] == a and "another open county call" in offer["why"]
    assert possibly(rig, p, a)


def test_another_kind_of_call_on_the_street_is_only_offered(tmp_path, monkeypatch):
    lost_number(monkeypatch)
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", "412 SAMPLE POST RD", ("PCFD",), typ="BLS")
    rig.at(60)
    p = rig.page("p1", PAGE.replace(", Sample Fitness", ""))["id"]
    assert p != a and "another kind of call" in state_inc(rig, p)["merge_offer"]["why"]


def test_another_town_named_never_joins(tmp_path, monkeypatch):
    lost_number(monkeypatch)
    rig = Rig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", "412 SAMPLE POST RD RYE_BROOK", ("PCFD",), typ="HAZARD/GAS")
    rig.at(60)
    p = rig.page("p1", PAGE.replace("Specimen Street.", "Specimen Street in Port Chester."))["id"]
    assert p != a and "merge_offer" not in state_inc(rig, p)


def test_said_towns(monkeypatch):
    from sizeup.cad import sources
    for name in ("TOWN_OF", "TOWN_NAME", "LISTED", "TOWNS"):              # put back after (module state)
        monkeypatch.setattr(sources, name, getattr(sources, name))
    sources.set_towns(["Port Chester", "Rye Brook", "Rye", "Harrison"], {"rye city": "Rye"})
    said = sources.Sources._said_towns
    assert said("respond to 412 sample post road in port chester. inside odor of gas") == ({"port chester"}, set())
    assert said("in the village of rye brook, 9 model street") == ({"rye brook"}, set())
    assert said("mutual aid to the city of rye, 9 model street") == ({"rye"}, set())
    assert said("engine 60 to harrison for a cover assignment") == ({"harrison"}, set())
    assert said("port chester fire department, respond to 9 model street") == (set(), set())     # who, not where
    assert said("port chester and rye brook fire departments, 9 model street") == (set(), set())
    assert said("sample fitness in rye brook avenue and specimen street") == (set(), set())     # a street of that name
    assert said("9 model street, rye brook, smoke showing") == (set(), {"rye brook"})           # named elsewhere


def test_over_5_min_after_the_dispatch_is_offered_and_over_10_never_scored(tmp_path, monkeypatch):
    """The 5-min rule decides a join (#365's review); up to WINDOW_S a person may still merge it."""
    lost_number(monkeypatch)
    for dt, offered in ((7 * 60, True), (pagematch.WINDOW_S + 60, False)):
        rig = Rig(tmp_path / str(dt))
        places(rig)
        a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="INVEST/GAS-ODOR")
        rig.at(dt)
        p = rig.page("p1", PAGE)["id"]
        assert p != a and ("merge_offer" in state_inc(rig, p)) is offered


def test_a_closed_county_event_is_never_scored(tmp_path, monkeypatch):
    lost_number(monkeypatch)
    rig = Rig(tmp_path)
    places(rig)
    feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="INVEST/GAS-ODOR")
    rig.feed.change("A1", times={"dispatched": rig.clock.t, "in_service": rig.clock.t + 30})
    rig.poll()
    rig.at(60)
    p = rig.page("p1", PAGE)["id"]
    assert paging_on(rig) == p and "merge_offer" not in state_inc(rig, p)


def test_other_units_on_each_side_are_offered_never_joined(tmp_path, monkeypatch):
    lost_number(monkeypatch)
    rig = Rig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("E62",), typ="INVEST/GAS-ODOR")
    rig.at(60)
    p = rig.page("p1", PAGE_UNITS)["id"]
    assert p != a and "not joined: other units" in state_inc(rig, p)["merge_offer"]["why"]


def test_a_page_not_for_us_never_reaches_the_matcher(tmp_path):
    rig = Rig(tmp_path)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="HAZARD/GAS")
    rig.at(60)
    inc = rig.monitor.on_page("p1", ["Other Town FD"], parse(PAGE.replace("Port Chester Fire", "Other Town Fire")),
                              at=rig.clock.t)
    assert rig.cad.from_monitor(inc) is None and paging_on(rig) is None
    assert [i["id"] for i in open_incidents(rig)] == [a]


# -- the review of #684: a false join hides a real call -------------------------------------------------------------

FIRE_AT = ("Port Chester Fire Department, Engine 60, Ladder 31, respond to a structure fire, {n} Sample Post Road, "
           "Sample Fitness in Example Avenue and Specimen Street. Time out 1431.")


def units_on(rig, iid) -> set:
    return {a["name"] for a in rig.cad.full(iid)["assignments"] if not a["cleared_at"]}


@pytest.mark.parametrize("mode", ["60control", "pcfd"])
@pytest.mark.parametrize("heard", ["418", "413"])
def test_the_house_next_door_is_another_call_in_both_modes(tmp_path, mode, heard):
    """418 or 413 for 412, on the street as typed, the business named: next door. Separate, no offer, and the page's
    units never go on the feed's call."""
    rig = AutoRig(tmp_path)
    rig.mode(mode)
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="STRU/COMM")
    rig.at(60)
    p = rig.page("p1", FIRE_AT.format(n=heard))["id"]
    assert p != a and paging_on(rig) == p and len(open_incidents(rig)) == 2
    assert "merge_offer" not in state_inc(rig, p)
    assert not units_on(rig, a) & {"E60", "L31"}


@pytest.mark.parametrize("stale", [False, True])
def test_a_fire_at_418_never_joins_the_alarm_the_feed_shows_at_412(tmp_path, stale):
    """The review's chain: the county's event for the fire at 418 isn't listed yet (late, or the feed backing off);
    the only open county call is an alarm at 412 on the same street. Never joined, no units on the wrong call."""
    rig = AutoRig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", "412 SAMPLE POST RD", ("E62",), typ="ALARM/COMM")
    if stale:
        rig.feed.outage("backing_off", "HTTP 429")
        rig.poll()
    rig.at(90)
    p = rig.page("p1", FIRE_AT.format(n="418").replace(", Sample Fitness", ""))["id"]
    assert p != a and paging_on(rig) == p
    assert "merge_offer" not in state_inc(rig, p)
    assert not units_on(rig, a) & {"E60", "L31"}


def test_a_stale_feed_never_joins_on_the_score(tmp_path, monkeypatch):
    """The scenario with the number lost (the business named: strong when fresh), the feed backing off: offered only."""
    lost_number(monkeypatch)
    rig = Rig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="INVEST/GAS-ODOR")
    rig.feed.outage("backing_off", "HTTP 429")
    rig.poll()
    rig.at(60)
    p = rig.page("p1", PAGE)["id"]
    assert p != a and "not joined: the county feed not fresh" in state_inc(rig, p)["merge_offer"]["why"]


@pytest.mark.parametrize("words", ["Engine 60, mutual aid to the City of Rye", "Engine 60, respond to the Town of Harrison"])
def test_a_mutual_aid_page_never_joins_our_call_at_that_address(tmp_path, words):
    """The same number and street as our open county call, but the page sends us elsewhere: another call, noted."""
    rig = AutoRig(tmp_path)
    a = feed_event(rig, "A1", "412 SAMPLE POST RD", ("PCFD",), typ="HAZARD/GAS")
    rig.at(60)
    p = rig.page("p1", f"Port Chester Fire Department, {words}, odor of gas, 412 Sample Post Road. Time out 1431.")["id"]
    assert p != a and paging_on(rig) == p and possibly(rig, p, a)
    assert "merge_offer" not in state_inc(rig, p) and "E60" not in units_on(rig, a)


def test_a_feed_event_with_no_house_number_is_offered_never_joined(tmp_path):
    """The county's location the street and the business, no number; the page "412" there and the business named."""
    rig = Rig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", "SAMPLE POST RD : @SAMPLE FITNESS", ("PCFD",), typ="INVEST/GAS-ODOR")
    rig.at(60)
    p = rig.page("p1", PAGE)["id"]
    assert p != a and "no house number from the county" in state_inc(rig, p)["merge_offer"]["why"]


def snapshot(rig) -> dict:
    """Everything but the offer: the incidents, their units, their timelines (a "possibly the same" note among them)."""
    q = rig.cad.store.q
    return {"incidents": [(i["id"], i["status"], i["number"], i["type"], i["address"]) for i in rig.incidents()],
            "units": [(r["incident_id"], r["unit_id"], r["cleared_at"]) for r in q("SELECT * FROM assignments ORDER BY id")],
            "events": [(e["incident_id"], e["kind"], e["text"]) for e in q("SELECT * FROM events ORDER BY id")],
            "reports": [(r["source"], r["incident_id"]) for r in q("SELECT * FROM source_reports ORDER BY source, first_seen")]}


@pytest.mark.parametrize("case", ["two calls", "late", "repeat"])
def test_a_weak_offer_changes_nothing_but_the_button(tmp_path, monkeypatch, case):
    """60 Control mode, as main without the score (page_feed None): the same cards, statuses, units, notes and
    reports after the page (its repeat: rematch_page) and after the feed's update (_sweepable); the offer is extra."""
    from sizeup.cad.sources import Sources
    lost_number(monkeypatch)

    def run(sub, scored):
        if not scored:
            monkeypatch.setattr(Sources, "page_feed", lambda self, *a, **k: None)
        rig = AutoRig(tmp_path / sub)
        places(rig)
        feed_event(rig, "A1", FEED_AT, ("E60",), typ="INVEST/GAS-ODOR")
        if case in ("two calls", "repeat"):                                     # (a second open call: weak)
            feed_event(rig, "A2", "9 MODEL ST", ("E62",), typ="ALARM/RESD", dt=30)
        rig.at(7 * 60 if case == "late" else 90)
        if case == "repeat":                                                     # the tones half first, no location
            rig.page("p1", "Port Chester Fire Department, full response, inside odor of gas.")
            rig.at(100)
        p = rig.page("p1" if case != "repeat" else "p2", PAGE)["id"]
        page = snapshot(rig)
        rig.at(8 * 60 if case == "late" else 120)
        rig.feed.change("A1", units=("E60", "L31"))
        rig.poll()
        offer = state_inc(rig, p).get("merge_offer")
        monkeypatch.undo() if not scored else None
        return page, snapshot(rig), offer
    page, after, offer = run("scored", True)
    lost_number(monkeypatch)
    page0, after0, offer0 = run("main", False)
    assert offer is not None and offer0 is None
    assert page == page0 and after == after0
    assert not any("Same call as" in e[2] for e in after["events"])


def test_undo_puts_back_the_cross_street_scope_and_audio(tmp_path, monkeypatch):
    rig, a, _b, _p = merge_rig(tmp_path, monkeypatch)
    with rig.cad.store.tx() as db:                                               # the page's clip, as the station logs it
        db.execute("INSERT INTO radio_traffic (id, start, end, channel, text, updated_at) VALUES "
                   "('p1', ?, ?, 'South 2', 'page', ?)", (rig.clock.t, rig.clock.t + 20, rig.clock.t))
    p = rig.page("p1", PAGE)["id"]
    rig.cad.comms.link_pages(p, ["p1"])
    before = (rig.cad.store.incident(a)["cross_street"], rig.cad.sources._scope_row(a),
              rig.cad.sources._page_links(a, ["p1"]))
    rig.cad.merge(p, {"into": a, "operator": "Lt. Test"})
    merged = rig.cad.store.incident(a)
    assert merged["cross_street"] and rig.cad.sources._page_links(a, ["p1"]) == ["p1"]
    assert rig.cad.sources._scope_row(a) != before[1]
    rig.cad.merge(p, {"undo": True, "operator": "Lt. Test"})
    after = (rig.cad.store.incident(a)["cross_street"], rig.cad.sources._scope_row(a),
             rig.cad.sources._page_links(a, ["p1"]))
    assert after == before
    assert rig.cad.sources._page_links(p, ["p1"]) == ["p1"]                      # still on the page's own card
    with pytest.raises(Exception, match="Not a merged page"):
        rig.cad.merge(p, {"undo": True})


def test_undo_refuses_when_the_page_moved_on(tmp_path, monkeypatch):
    from sizeup.cad.store import CadError
    rig, a, b, p = merge_rig(tmp_path, monkeypatch)
    rig.cad.merge(p, {"into": a})
    with rig.cad.store.tx() as db:                                               # moved on since (a feed's merge)
        db.execute("UPDATE source_reports SET incident_id = ? WHERE source = 'paging'", (b,))
    with pytest.raises(CadError, match="no longer on"):
        rig.cad.merge(p, {"undo": True})
    assert rig.cad.store.incident(p)["status"] == "dismissed"


def test_a_repeat_naming_the_place_joins_through_rematch(tmp_path, monkeypatch):
    lost_number(monkeypatch)
    rig = AutoRig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("E60",), typ="INVEST/GAS-ODOR")
    rig.at(90)
    first = rig.page("p1", "Port Chester Fire Department, full response, inside odor of gas.")["id"]
    rig.at(100)
    assert rig.page("p2", PAGE)["id"] == a and rig.cad.store.incident(first)["status"] == "dismissed"
    assert any(" matched by: " in x for x in feed_lines(rig, a))


def test_the_maps_cross_streets_are_never_looked_up_under_the_lock(tmp_path, monkeypatch):
    """Only when the page says cross streets, and then on a thread of its own: the first page gets none."""
    import threading
    lost_number(monkeypatch)
    rig = Rig(tmp_path)
    places(rig)
    asked, go = [], threading.Event()

    def detail(m):
        asked.append(threading.current_thread().name)
        go.wait(5)
        return {"cross": "Example Ave and Specimen St"}
    rig.cad.detail_source = detail
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="INVEST/GAS-ODOR")
    with rig.cad.store.tx() as db:
        db.execute("UPDATE incidents SET lat = 41.0, lon = -73.66 WHERE id = ?", (a,))
    rig.at(60)
    src = rig.cad.sources
    nocross = {"f": {"type": "HAZARD", "subtype": "GAS"}, "lines": [PAGE], "towns": []}
    src.page_feed(rig.clock.t, "Sample Post Road", nocross)
    assert asked == []                                                           # no cross streets said: never asked
    inc = rig.cad.store.incident(a)
    assert src._block_cross(inc) == ""                                           # asked, not waited for
    go.set()
    for _ in range(100):
        if src._block_cross(inc):
            break
        threading.Event().wait(0.01)
    assert src._block_cross(inc) == "Example Ave and Specimen St" and asked == ["page-cross"]


# -- the merge a person makes, and its undo --------------------------------------------------------------------------


def merge_rig(tmp_path, monkeypatch, cls=Rig):
    lost_number(monkeypatch)
    rig = cls(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("PCFD",), typ="INVEST/GAS-ODOR")
    b = feed_event(rig, "A2", "9 MODEL ST", ("PCFD",), typ="ALARM/RESD", dt=30)
    rig.at(90)
    p = rig.page("p1", PAGE)["id"]
    return rig, a, b, p


def test_merge_is_for_dispatchers_audited_and_undone(tmp_path, monkeypatch):
    from tests.test_roles import JORDAN, VAL, people
    rig, a, _b, p = merge_rig(tmp_path, monkeypatch)
    api = CadApi(rig.cad)
    people(rig.cad)
    assert needs(f"/api/cad/incident/{p}/merge") == "dispatch"

    def post(body, email):
        code, raw, _ = api.post(f"/api/cad/incident/{p}/merge", json.dumps(body).encode(), Identity(email=email))
        return code, json.loads(raw)
    code, _ = post({"into": a}, VAL)
    assert code == 403 and rig.cad.store.incident(p)["status"] == "queued"
    code, out = post({"into": a}, JORDAN)
    assert code == 200 and out["id"] == a
    assert rig.cad.store.incident(p)["status"] == "dismissed" and paging_on(rig) == a
    assert any(x.startswith(f"Page card #{p} merged into this by") for x in feed_lines(rig, a))
    assert state_inc(rig, p)["merged_into"]["into"] == a
    assert any(r["summary"].startswith("Merged a 60 Control page into") for r in rig.cad.activity.rows(email=JORDAN))
    code, out = post({"undo": True}, JORDAN)
    assert code == 200 and out["status"] == "queued" and paging_on(rig) == p
    assert any("taken back out" in x for x in feed_lines(rig, a))
    assert state_inc(rig, p)["merge_offer"]["into"] == a                         # offered again
    assert any(r["summary"].startswith("Undid the merge") for r in rig.cad.activity.rows(email=JORDAN))


def test_merge_is_refused_for_a_card_with_units_or_an_incident_with_no_feed(tmp_path, monkeypatch):
    from sizeup.cad.store import CadError
    rig, a, _b, p = merge_rig(tmp_path, monkeypatch)
    with pytest.raises(CadError):
        rig.cad.merge(p, {"into": p})
    manual = rig.cad.create({"type": "STRU", "address": "5 Pattern Ln", "operator": "Lt. Test"})
    with pytest.raises(CadError):
        rig.cad.merge(p, {"into": manual["id"]})
    rig.cad.store.assign(p, [rig.uid("E60")], operator="Lt. Test", attach=True)
    with pytest.raises(CadError, match="clear them first"):
        rig.cad.merge(p, {"into": a})
    assert rig.cad.store.incident(p)["status"] == "queued" and paging_on(rig) == p


def test_a_strong_join_in_pcfd_mode_dispatches_nobody(tmp_path, monkeypatch):
    lost_number(monkeypatch)
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("E60", "L31"), typ="INVEST/GAS-ODOR")
    rig.at(60)
    assert rig.page("p1", PAGE_UNITS)["id"] == a
    assert rig.cad.full(a)["assignments"] == [] and rig.status("E60") == rig.status("L31")


def test_a_strong_join_in_60_control_mode_is_the_page_path(tmp_path, monkeypatch):
    """As any page that joins a feed's incident (test_street_only): the units it named there, nothing else."""
    lost_number(monkeypatch)
    rig = AutoRig(tmp_path)
    places(rig)
    a = feed_event(rig, "A1", FEED_AT, ("E60", "L31"), typ="INVEST/GAS-ODOR")
    rig.at(60)
    assert rig.page("p1", PAGE_UNITS)["id"] == a and len(open_incidents(rig)) == 1
    assert {x["name"] for x in rig.cad.full(a)["assignments"]} <= {"E60", "L31"}


# -- the decision on its own -----------------------------------------------------------------------------------------


def ev(**kw):
    return pagematch.Evidence(inc={"id": kw.pop("id", 1)}, **kw)


def test_decide():
    """Strong needs the place itself (its name, or its number exact or said twice); everything else only supports."""
    ok = dict(street="same", feed_numbered=True, fresh=True)
    d = lambda **kw: pagematch.decide([ev(**{**ok, **kw})])
    assert pagematch.STRONG_S == SAME_CALL_S
    assert d(place="Sample Fitness").decision == "strong"
    assert d(number="exact").decision == "strong" and d(number="doubled").decision == "strong"
    assert d(place="Sample Fitness", lag_s=-50).decision == "strong"
    for support in (dict(family="same"), dict(cross="match"), dict(family="same", cross="match"), {}):
        assert d(**support).decision == "weak"                                        # never enough on its own
    assert d(place="Sample Fitness", fresh=False).decision == "weak"                    # the feed stale or backing off
    assert d(place="Sample Fitness", feed_numbered=False).decision == "weak"            # the county gave no number
    assert d(place="Sample Fitness", lag_s=7 * 60).decision == "weak"
    assert d(place="Sample Fitness", taken=True).decision == "weak"
    assert d(place="Sample Fitness", cross="differ").decision == "weak"
    assert d(place="Sample Fitness", units="differ").decision == "weak"
    assert d(street="snapped", place="Sample Fitness", number="exact").decision == "weak"   # a misheard street
    assert d(family="differ", units="differ") is None                                   # two things against
    for n in ("near", "misread", "other"):                                              # the street as typed: next door
        assert d(place="Sample Fitness", number=n) is None
    assert d(street="snapped", number="near").decision == "weak"                        # misheard: "near" neutral
    assert d(street="snapped", number="misread").decision == "weak"
    assert d(street="snapped", number="other") is None
    assert d(place="Sample Fitness", town="differ") is None
    assert d(place="Sample Fitness", mutual="differ") is None
    assert pagematch.decide([ev(family="same", place="Sample Fitness")]) is None          # another street
    two = pagematch.decide([ev(id=1, **ok, place="Sample Fitness"), ev(id=2)])
    assert two.decision == "weak" and two.inc["id"] == 1


def test_exact_street_and_mutual_aid():
    st = lambda x: __import__("sizeup.cad.sources", fromlist=["x"]).place_key(x)[2:]
    assert pagematch.exact_street(st("412 Sample Post Road"), st("412 SAMPLE POST RD"))
    assert not pagematch.exact_street(st("412 Sample Post"), st("412 SAMPLE POST RD"))      # no suffix said
    assert not pagematch.exact_street(st("412 N Sample Rd"), st("412 SAMPLE RD"))
    assert pagematch.mutual_aid("Engine 60, mutual aid to the City of Sample") and pagematch.mutual_aid("", "MA")
    assert not pagematch.mutual_aid("respond to a gas leak", "HAZARD")


def test_number_fit_and_families():
    fit = pagematch.number_fit
    assert fit("412", "412") == "exact" and fit("412412", "412", "412") == "doubled"
    assert fit("4120", "412") == "misread" and fit("413", "412") == "near" and fit("88", "412") == "other"
    assert fit("", "412") == ""
    gas = pagematch.families
    assert gas("HAZARD", "GAS") & gas("INVEST", "GAS-ODOR") and not gas("HAZARD", "GAS") & gas("BLS")
    assert gas("ALARM", "CO-RESD") & gas("INVEST", "CO-YES") and not gas("ALARM", "CO-RESD") & gas("HAZARD", "GAS")
    assert gas("DRILL") == frozenset() and gas("") == frozenset()
    assert not gas("ALARM", "RESD") & gas("MVF", "CAR")                                 # a car fire is no building's alarm


def test_says_name():
    words = pagematch.name_words("respond 412 sample post road, sample fitness in example avenue")
    assert pagematch.says_name(words, "SAMPLE FITNESS") and not pagematch.says_name(words, "SPECIMEN DINER")
    assert not pagematch.says_name(pagematch.name_words("the ace was"), "ACE")              # one short word: too loose
