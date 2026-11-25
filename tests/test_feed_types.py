"""A feed's own type code mapped to ours (sizeup/cad/feedtypes.py, Cad.map_type): the SUB of a county "MAIN,SUB" beside
our TYPE picks that type's one subtype it names (code or name, case and punctuation aside, a plural, a short form), never
another type's; none or two: no subtype and one timeline line. Settings > Lists > Feed types: the department's rows win,
and the codes nothing maps fully are listed with what they map to now. A page's words name a subtype of the mapped type
the same way. Synthetic only: invented codes ("SERVICE,SAMPLE"), places, units and times (FakeFeed)."""

from __future__ import annotations

import json

import pytest

from sizeup.cad import feedtypes
from sizeup.cad.store import CadError
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0, Rig

LABEL = "County feed"


def post(cad, section, **body):
    return cad.settings_post(section, dict(body, operator=body.get("operator", "JD")))


def add_subtype(cad, typ: str, code: str, name: str) -> None:
    """What an admin does in Settings > Lists: a new subtype, allowed under one type."""
    post(cad, "event_subtypes", action="add", code=code, item={"name": name})
    post(cad, "type_subtypes", action="set", type=typ, subtypes=[*cad.lists.type_subtypes.get(typ, ()), code])


def county(rig, eid="A1", sent="SERVICE", code="SERVICE,SAMPLE", location="40 EXAMPLE ST", units=("E60",), dt=0.0):
    """The adapter's event: our TYPE when its table knows the MAIN, the county's own code in extra."""
    rig.feed.put(eid, type=sent, location=location, units=units, times={"dispatched": T0 + dt},
                 extra={"county_type": code} if code else {})
    rig.poll()
    return rig.cad.store.one("SELECT incident_id FROM source_reports WHERE source = 'county' AND ref = ?",
                             (eid,))["incident_id"]


@pytest.fixture
def rig(tmp_path):
    return AutoRig(tmp_path)


# -- matching a subtype word under one type -----------------------------------------------------------------------------


def test_a_subtype_word_matches_by_code_name_plural_or_short_form_under_its_type_only(tmp_path):
    L = Rig(tmp_path).cad.lists
    assert L.match_subtype("SERVICE", "SPRINK") == ("SPRINK", ("SPRINK",))          # the code
    assert L.match_subtype("SERVICE", "sprinkler")[0] == "SPRINK"                    # the name, any case
    assert L.match_subtype("SERVICE", " Sprinklers ")[0] == "SPRINK"                 # a plural
    assert L.match_subtype("SERVICE", "APPLIANCE")[0] == "APPLIANC"                  # the code a short form of it
    assert L.match_subtype("SERVICE", "MISC.")[0] == "MISC"                          # punctuation aside
    assert L.match_subtype("ALARM", "CO RESD")[0] == "CO-RESD"                       # spaces and dashes aside
    assert L.match_subtype("SERVICE", "WATER") == ("", ())                           # another type's: never
    assert L.match_subtype("SERVICE", "") == ("", ()) and L.match_subtype("BLS", "SPRINK") == ("", ())
    assert L.match_subtype("NOSUCH", "SPRINK") == ("", ())


def test_two_subtypes_matching_is_none(tmp_path):
    cad = Rig(tmp_path).cad
    add_subtype(cad, "SERVICE", "SAMPLEA", "Sample one")
    add_subtype(cad, "SERVICE", "SAMPLEB", "Sample two")
    assert cad.lists.match_subtype("SERVICE", "SAMP") == ("", ("SAMPLEA", "SAMPLEB"))
    assert cad.lists.match_subtype("SERVICE", "SAMPLEB")[0] == "SAMPLEB"            # the same one first


# -- Cad.map_type with the feed's own code ------------------------------------------------------------------------------


def test_our_type_and_the_county_sub_pick_the_one_subtype(tmp_path):
    cad = Rig(tmp_path).cad
    m = lambda sent, code: cad.map_type(sent, LABEL, source="county", county=code)
    assert m("SERVICE", "SERVICE,SAMPLE") == ("SERVICE", "", f"{LABEL} subtype SAMPLE not in Service's subtypes")
    add_subtype(cad, "SERVICE", "SAMPLE", "Sample")
    assert m("SERVICE", "SERVICE,SAMPLE") == ("SERVICE", "SAMPLE", "")
    assert m("SERVICE", "SERVICE , samples") == ("SERVICE", "SAMPLE", "")             # spaces, case, a plural
    assert m("SERVICE", "SERVICE") == ("SERVICE", "", "")                            # no SUB: nothing to say
    assert m("ALARM/COMM", "ALARM,SAMPLE") == ("ALARM", "COMM", "")                  # the adapter's full code stands


def test_a_sub_that_is_another_types_subtype_is_never_taken(tmp_path):
    cad = Rig(tmp_path).cad
    add_subtype(cad, "RESCUE", "SAMPLE", "Sample")
    typ, sub, note = cad.map_type("SERVICE", LABEL, source="county", county="SERVICE,SAMPLE")
    assert (typ, sub) == ("SERVICE", "") and note == f"{LABEL} subtype SAMPLE not in Service's subtypes"
    assert feedtypes.is_miss(note)


def test_ambiguous_sub_is_none_with_the_matches_said(tmp_path):
    cad = Rig(tmp_path).cad
    add_subtype(cad, "SERVICE", "SAMPLEA", "Sample one")
    add_subtype(cad, "SERVICE", "SAMPLEB", "Sample two")
    typ, sub, note = cad.map_type("SERVICE", LABEL, source="county", county="SERVICE,SAMP")
    assert (typ, sub) == ("SERVICE", "")
    assert note == f"{LABEL} subtype SAMP matches several of Service's subtypes: SAMPLEA, SAMPLEB" and feedtypes.is_miss(note)


def test_type_sub_text_of_our_type_keeps_the_type(tmp_path):
    """A feed with no table: "TYPE,SUB" / "TYPE/SUB" with our TYPE and a SUB it doesn't allow kept the type out (read
    like a page's words); now the type stands and its one subtype the SUB names, else none and the note."""
    cad = Rig(tmp_path).cad
    assert cad.map_type("SERVICE,SPRINKLER", LABEL) == ("SERVICE", "SPRINK", "")
    assert cad.map_type("SERVICE/sprinklers", LABEL) == ("SERVICE", "SPRINK", "")
    assert cad.map_type("SERVICE,SAMPLE", LABEL) == ("SERVICE", "", f"{LABEL} subtype SAMPLE not in Service's subtypes")
    assert cad.map_type("BLS,SAMPLE", LABEL) == ("BLS", "", "")                     # a type with no subtypes: quiet
    assert cad.map_type("TYPEA,SUBB", LABEL)[:2] == ("", "")                        # nobody's code: still a person's


# -- Settings > Lists > Feed types --------------------------------------------------------------------------------------


def test_a_feed_types_row_wins_over_the_automatic_mapping(tmp_path):
    cad = Rig(tmp_path).cad
    post(cad, "feed_types", action="set", feed="county", code="service , sample", type="SERVICE", subtype="MISC")
    assert cad.map_type("SERVICE", LABEL, source="county", county="SERVICE,SAMPLE") == ("SERVICE", "MISC", "")
    assert cad.map_type("SERVICE", LABEL, source="other", county="SERVICE,SAMPLE")[1] == ""   # that feed's only
    post(cad, "feed_types", action="set", feed="county", code="ALARM,SAMPLE", type="ALARM", subtype="RESD")
    assert cad.map_type("ALARM/COMM", LABEL, source="county", county="ALARM,SAMPLE") == ("ALARM", "RESD", "")
    # a MAIN alone: its type for every SUB, the SUB still matched under it
    post(cad, "feed_types", action="set", feed="county", code="ZZMAIN", type="SERVICE", subtype="")
    assert cad.map_type("ZZMAIN,SPRINKLER", LABEL, source="county", county="ZZMAIN,SPRINKLER") == ("SERVICE", "SPRINK", "")
    assert cad.map_type("ZZMAIN,SAMPLE", LABEL, source="county", county="ZZMAIN,SAMPLE")[:2] == ("SERVICE", "")
    page = cad.settings_get()["feed_types"]
    assert {(r["source"], r["code"], r["type"], r["subtype"]) for r in page["rows"]} == {
        ("county", "SERVICE,SAMPLE", "SERVICE", "MISC"), ("county", "ALARM,SAMPLE", "ALARM", "RESD"),
        ("county", "ZZMAIN", "SERVICE", "")}
    audit = cad.settings.audit_log("lists")
    assert any(a["key"] == "feed_types/county|SERVICE,SAMPLE" for a in audit)
    post(cad, "feed_types", action="reset", key="county|SERVICE,SAMPLE")
    assert cad.map_type("SERVICE", LABEL, source="county", county="SERVICE,SAMPLE")[1] == ""


def test_a_feed_types_row_is_checked_and_one_the_lists_no_longer_allow_is_passed_over(tmp_path):
    cad = Rig(tmp_path).cad
    for bad in (dict(feed="county", code="X", type="NOSUCH"), dict(feed="county", code="X", type="SERVICE", subtype="COMM"),
                dict(feed="County Feed", code="X", type="SERVICE"), dict(feed="county", code=" , ", type="SERVICE"),
                dict(feed="county", code="X", type="")):
        with pytest.raises(CadError):
            post(cad, "feed_types", action="set", **bad)
    add_subtype(cad, "SERVICE", "SAMPLE", "Sample")
    post(cad, "feed_types", action="set", feed="county", code="ZZMAIN,ZZSUB", type="SERVICE", subtype="SAMPLE")
    post(cad, "type_subtypes", action="set", type="SERVICE",
         subtypes=[s for s in cad.lists.type_subtypes["SERVICE"] if s != "SAMPLE"])
    assert cad.map_type("ZZMAIN,ZZSUB", LABEL, source="county", county="ZZMAIN,ZZSUB")[:2] == ("", "")
    assert cad.settings_get()["feed_types"]["rows"][0]["off"]                      # shown as not applied


def test_feed_types_go_through_export_and_import(tmp_path):
    cad = Rig(tmp_path).cad
    post(cad, "feed_types", action="set", feed="county", code="SERVICE,SAMPLE", type="SERVICE", subtype="MISC")
    data = cad.settings_export()
    assert data["overrides"]["feed_types"] == {"county|SERVICE,SAMPLE": {"type": "SERVICE", "subtype": "MISC"}}
    post(cad, "feed_types", action="reset", key="county|SERVICE,SAMPLE")
    post(cad, "import", action="apply", settings=data)
    assert cad.map_type("SERVICE", LABEL, source="county", county="SERVICE,SAMPLE")[1] == "MISC"
    data["overrides"]["feed_types"] = {"county|service,sample": {"type": "SERVICE"}}          # not a row's key
    with pytest.raises(CadError):
        post(cad, "import", action="dry_run", settings=data)


# -- end to end: the feed's event, the timeline, the seen codes ---------------------------------------------------------


def test_the_county_sub_sets_the_subtype_on_the_incident(rig):
    add_subtype(rig.cad, "SERVICE", "SAMPLE", "Sample")
    a = county(rig)
    inc = rig.cad.store.incident(a)
    assert (inc["type"], inc["subtype"], inc["type_note"]) == ("SERVICE", "SAMPLE", "")
    assert rig.cad.settings_get()["feed_types"]["seen"] == []                      # mapped in full: not listed


def test_a_sub_nothing_maps_is_one_timeline_line_and_listed_until_mapped(rig):
    a = county(rig)
    inc = rig.cad.store.incident(a)
    miss = f"{LABEL} subtype SAMPLE not in Service's subtypes"
    assert (inc["type"], inc["subtype"], inc["type_note"]) == ("SERVICE", "", miss)
    assert rig.feed_lines(a).count(miss) == 1
    rig.at(60)
    rig.feed.change("A1", units=("E60", "L31"))
    rig.poll()
    assert rig.feed_lines(a).count(miss) == 1                                      # once
    seen = rig.cad.settings_get()["feed_types"]["seen"]
    assert [(r["source"], r["code"], r["n"], r["state"], r["type"]) for r in seen] == [
        ("county", "SERVICE,SAMPLE", 1, "partly", "SERVICE")]
    county(rig, eid="B2", code="ZZMAIN,ZZSUB", sent="ZZMAIN,ZZSUB", location="7 SAMPLE AVE", dt=90)
    seen = rig.cad.settings_get()["feed_types"]["seen"]
    assert [(r["code"], r["state"]) for r in seen] == [("ZZMAIN,ZZSUB", "unmapped"), ("SERVICE,SAMPLE", "partly")]
    assert rig.cad.settings_get()["feed_types"]["feeds"][0]["source"] == "county"
    # the admin's Map...: a row for the code; it is off the list, and the next event takes it
    post(rig.cad, "feed_types", action="set", feed="county", code="SERVICE,SAMPLE", type="SERVICE", subtype="MISC")
    assert [r["code"] for r in rig.cad.settings_get()["feed_types"]["seen"]] == ["ZZMAIN,ZZSUB"]
    c = county(rig, eid="C3", location="12 SPECIMEN RD", dt=200)
    assert (rig.cad.store.incident(c)["type"], rig.cad.store.incident(c)["subtype"]) == ("SERVICE", "MISC")
    assert rig.cad.store.one("SELECT n FROM feed_types_seen WHERE code = 'SERVICE,SAMPLE'")["n"] == 2


def test_a_new_county_sub_under_the_same_type_is_mapped_again(rig):
    add_subtype(rig.cad, "SERVICE", "SAMPLE", "Sample")
    a = county(rig, code="SERVICE,OTHERWORD")
    assert rig.cad.store.incident(a)["subtype"] == ""
    rig.at(60)
    rig.feed.change("A1", extra={"county_type": "SERVICE,SAMPLE"})
    rig.poll()
    assert rig.cad.store.incident(a)["subtype"] == "SAMPLE"


# -- a page's words -----------------------------------------------------------------------------------------------------


SPRINKLER_PAGE = ("Port Chester Fire Department, Engine 60, respond for a water condition from a sprinkler, 12 Example "
                  "Avenue, cross of Sample Street. Time out 1432.")


def test_a_page_naming_one_subtype_of_its_type(rig):
    got = rig.page("p1", SPRINKLER_PAGE)
    inc = rig.cad.store.incident(got["id"])
    assert (inc["type"], inc["subtype"]) == ("SERVICE", "SPRINK")
    two = SPRINKLER_PAGE.replace("from a sprinkler", "with no heat and a sprinkler")
    got = rig.page("p2", two.replace("12 Example Avenue", "400 Sample Street"), fresh=True)
    assert (rig.cad.store.incident(got["id"])["type"], rig.cad.store.incident(got["id"])["subtype"]) == ("SERVICE", "")


def test_a_page_names_the_subtype_of_the_feeds_type(rig):
    a = county(rig, code="SERVICE,SAMPLE", location="9 EXAMPLE TER")
    assert rig.cad.store.incident(a)["subtype"] == ""
    rig.at(28)
    page = ("Port Chester Fire Department, Engine 60, respond for a sprinkler problem, Example Terrace, between Sample "
            "Avenue and Specimen Street. Time out 1432.")
    got = rig.page("p1", page)
    assert got["id"] == a
    inc = rig.cad.store.incident(a)
    assert (inc["type"], inc["subtype"], inc["type_note"]) == ("SERVICE", "SPRINK", "")
    rig.at(60)                                                     # the feed's next report keeps the page's subtype
    rig.feed.change("A1", units=("E60", "L31"))
    rig.poll()
    assert rig.cad.store.incident(a)["subtype"] == "SPRINK"


def test_a_page_never_names_another_types_subtype_nor_its_place(rig):
    a = county(rig, code="SERVICE,SAMPLE", location="9 EXAMPLE TER")
    rig.at(28)
    page = ("Port Chester Fire Department, Engine 60, respond for a water problem, Example Terrace, between Sample "
            "Avenue and Specimen Street. Time out 1432.")                     # Water: RESCUE's and NOTIF's, not SERVICE's
    rig.page("p1", page)
    assert rig.cad.store.incident(a)["subtype"] == ""


# -- a call left open: the feed's close lost, or applied late -----------------------------------------------------------


def test_an_open_call_the_feed_stops_listing_asks_close(rig):
    """The feed's own close never arrived (dropped off its list): the call stayed Active with only a timeline line."""
    from sizeup.sources.runner import SEEN_EVERY_S
    rig.created()
    iid = rig.iid()
    assert rig.inc()["status"] == "active"
    del rig.feed.events["A1"]                                   # gone from the snapshot, never reported closed
    for k in range(1, 4):
        rig.at(k * 600 + SEEN_EVERY_S)
        rig.poll()
    inc = rig.inc()
    assert inc["status"] == "active"                            # never closed by itself
    ask = inc["auto"]["close_pending"]
    assert ask is not None and "no longer lists it: close?" in ask["text"]
    asks = rig.cad.store.q("SELECT 1 FROM auto_steps WHERE incident_id = ? AND kind = 'close?'", (iid,))
    assert len(asks) == 1                                       # once


def test_an_on_time_report_applied_late_by_the_hub_still_closes_the_call(rig):
    """The station had the report at once; the hub applied it after the assign window (a backlog): its in service closed
    nothing, only asked. Late is now judged by when the station first had it."""
    ev = {"source": "county", "event_id": "A1", "type": "ALARM/COMM", "location": "12 Example Ave", "units": ["E60"],
          "times": {"dispatched": T0}, "unit_times": {}, "comments": [], "closed": False, "extra": {}, "aliases": []}
    rig.at(25 * 60)
    rig.cad.source_event({"source": "county", "label": LABEL, "rank": 1, "seen_at": T0 + 30, "first_seen": T0 + 30,
                          "event": ev})
    assert rig.inc()["status"] == "active"
    rig.at(26 * 60)
    done = dict(ev, closed=True, times={"dispatched": T0, "in_service": T0 + 26 * 60 - 10})
    rig.cad.source_event({"source": "county", "label": LABEL, "rank": 1, "seen_at": T0 + 26 * 60, "first_seen": T0 + 30,
                          "event": done})
    assert rig.inc()["status"] == "closed"


def test_a_report_first_seen_late_by_the_station_still_only_asks(rig):
    ev = {"source": "county", "event_id": "A1", "type": "ALARM/COMM", "location": "12 Example Ave", "units": ["E60"],
          "times": {"dispatched": T0}, "unit_times": {}, "comments": [], "closed": False, "extra": {}, "aliases": []}
    rig.at(25 * 60)
    rig.cad.source_event({"source": "county", "label": LABEL, "rank": 1, "seen_at": T0 + 25 * 60,
                          "first_seen": T0 + 25 * 60, "event": ev})
    rig.at(26 * 60)
    done = dict(ev, closed=True, times={"dispatched": T0, "in_service": T0 + 26 * 60 - 10})
    rig.cad.source_event({"source": "county", "label": LABEL, "rank": 1, "seen_at": T0 + 26 * 60,
                          "first_seen": T0 + 25 * 60, "event": done})
    inc = rig.inc()
    assert inc["status"] == "active" and inc["auto"]["close_pending"] is not None


# -- review: what the first cut got wrong -------------------------------------------------------------------------------


def test_a_close_waiting_for_a_unit_still_goes_ahead_when_the_feed_stops_listing_it(rig):
    from sizeup.sources.runner import SEEN_EVERY_S
    rig.created()
    rig.at(300)
    rig.change(units=("E60",))                                  # L31 dropped by the county: still on the call here
    rig.at(900)
    rig.change(times={"in_service": T0 + 890})                  # in service, not closed: "close? (still on it: L31)"
    assert rig.inc()["status"] == "active" and rig.here("L31")
    del rig.feed.events["A1"]                                   # then off the county's list
    rig.at(900 + SEEN_EVERY_S + 60)
    rig.poll()
    assert "no longer lists it" not in rig.inc()["auto"]["close_pending"]["text"]
    rig.cad.unit_status(rig.uid("L31"), {"status": "AV", "operator": "Lt. Test"})     # they clear L31
    assert rig.inc()["status"] == "closed"


def test_a_sub_the_feed_changes_to_one_naming_none_clears_the_subtype_and_says_so(rig):
    add_subtype(rig.cad, "SERVICE", "SAMPLE", "Sample")
    a = county(rig)
    assert rig.cad.store.incident(a)["subtype"] == "SAMPLE"
    rig.at(60)
    rig.feed.change("A1", extra={"county_type": "SERVICE,OTHERWORD"})
    rig.poll()
    inc = rig.cad.store.incident(a)
    assert (inc["type"], inc["subtype"]) == ("SERVICE", "")
    assert f"{LABEL} subtype OTHERWORD not in Service's subtypes" in rig.feed_lines(a)


def test_a_code_mapped_to_no_subtype_or_whose_type_has_none_is_not_listed(rig):
    county(rig)
    county(rig, eid="B2", sent="BLS", code="BLS,SAMPLE", location="7 SAMPLE AVE", dt=60)
    assert [r["code"] for r in rig.cad.settings_get()["feed_types"]["seen"]] == ["SERVICE,SAMPLE"]
    post(rig.cad, "feed_types", action="set", feed="county", code="SERVICE,SAMPLE", type="SERVICE", subtype="")
    assert rig.cad.settings_get()["feed_types"]["seen"] == []


def test_a_row_matches_a_feeds_type_text_when_it_sends_no_county_code(tmp_path):
    cad = Rig(tmp_path).cad
    post(cad, "feed_types", action="set", feed="county", code="ZZMAIN,ZZSUB", type="INVEST", subtype="")
    assert cad.map_type("ZZMAIN / zzsub", LABEL, source="county") == ("INVEST", "", "")


def test_a_short_form_must_be_half_the_word_at_least(tmp_path):
    L = Rig(tmp_path).cad.lists
    assert L.match_subtype("ALARM", "COMMUNICATIONS") == ("", ())
    assert L.match_subtype("SERVICE", "SPRINK")[0] == "SPRINK"


def test_an_imported_row_the_lists_dont_allow_loads_and_shows_not_applied(tmp_path):
    cad = Rig(tmp_path).cad
    data = cad.settings_export()
    data["overrides"]["feed_types"] = {"county|ZZMAIN": {"type": "SERVICE", "subtype": "COMM"}}
    post(cad, "import", action="apply", settings=data)
    rows = cad.settings_get()["feed_types"]["rows"]
    assert [(r["code"], bool(r["off"])) for r in rows] == [("ZZMAIN", True)]
    assert cad.map_type("ZZMAIN", LABEL, source="county", county="ZZMAIN")[:2] == ("", "")


def test_lateness_of_a_co_run_is_read_from_its_own_report(rig):
    """_seen_open's key for a second run is "<source>#<ref>": its first_seen is still found by the source alone."""
    rig.created()
    iid = rig.iid()
    seen = rig.cad.store.one("SELECT first_seen FROM source_reports WHERE source = 'county' AND ref = 'A1'")["first_seen"]
    rig.at(30 * 60)                                             # the hub much later than the station's first sight
    data = rig.cad.store.one("SELECT data FROM source_reports WHERE ref = 'A1'")["data"]
    assert rig.cad.auto._seen_open(iid, "county#A1", json.loads(data)) is True
    assert seen == T0
