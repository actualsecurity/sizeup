"""An incident's address is stored normalised (sources.normal_location) and the text it came from is kept beside it
(incidents.address_raw) for matching and audit (sources.match_address). Every view reads the stored address; matching
reads exactly what it read before. Old rows are backfilled once (Sources.backfill_addresses). Synthetic data only:
made-up streets, towns from the data file's own list, made-up event ids and times."""

from __future__ import annotations

import pytest

from sizeup.cad import dispatcher, mapview
from sizeup.cad import sources as src
from sizeup.cad.sources import match_address, normal_location, same_place, same_place_halved, shown_location, town_label
from tests.test_incident_sources import T0, Rig, towns
from tests.test_street_only import open_incidents, possibly

STREETS = {"rye brook": ["Example Ave", "Sample Ln", "DeSample Rd", "N Example St"],
           "port chester": ["Exampleton Rd", "Sample Ave", "Specimen St", "North St"]}


@pytest.fixture(autouse=True)
def streets():
    """A made-up street list (the gazetteer's: Cad.street_source), the data file's towns; restored after."""
    old = src.STREETS
    towns()
    src.set_streets(STREETS)
    yield
    src.STREETS = old


def snake(t: str) -> str:
    return t.upper().replace(" ", "_")


# -- the form ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,kw,want", [
    ("40 EXAMPLE AVE rye brook", {}, "40 Example Ave, Rye Brook"),                 # ALL CAPS street, lower-case town
    ("40 EXAMPLE AVE RYE_BROOK", {"typed": True}, "40 Example Ave, Rye Brook"),     # the county's town token
    ("12 SAMPLE LN", {}, "12 Sample Ln, Rye Brook"),                               # no town: the one town whose streets have it
    ("12 SPECIMEN STREET", {"typed": True, "fallback": "Rye Brook"}, "12 Specimen St, Port Chester"),   # streets beat jurisdiction
    ("12 OTHER ST", {"typed": True, "fallback": "Rye Brook"}, "12 Other St, Rye Brook"),               # ... else the jurisdiction
    ("12 OTHER ST", {"geo_label": "12 Other St, Port Chester"}, "12 Other St, Port Chester"),           # the map's town
    ("EXAMPLETON RD/SAMPLE AVE OTHERTOWN", {"typed": True}, "Exampleton Rd & Sample Ave, Othertown"),  # out of area
    ("EXAMPLETON RD/SAMPLE AVE OTHERTOWN", {"typed": True, "town": "Othertown"}, "Exampleton Rd & Sample Ave, Othertown"),
    ("12 SAMPLE CT :3RD FL REAR OTHER_V", {"typed": True}, "12 Sample Ct, Other"),       # a village's token
    ("12 SAMPLE DR OTHER_T", {"typed": True}, "12 Sample Dr, Town of Other"),
    ("12 SAMPLE AVE : @SAMPLE SCHOOL - FRONT (E1) OTHER_V", {"typed": True}, "12 Sample Ave, Other"),
    ("12 NORTH EXAMPLE STREET RYE_BROOK", {}, "12 N Example St, Rye Brook"),       # suffix and direction abbreviated
    ("12 NORTH ST", {}, "12 North St, Port Chester"),                               # a direction that is the name stays
    ("8 SAMPLEWOOD AVENUE EXTENSION WEST", {}, "8 Samplewood Ave Ext W"),
    ("4 DESAMPLE ROAD", {}, "4 DeSample Rd, Rye Brook"),                            # the gazetteer's spelling
    ("12 MCSAMPLE PL", {}, "12 McSample Pl"), ("12 O'SAMPLE BOULEVARD", {}, "12 O'Sample Blvd"),
    ("5 1ST ST", {}, "5 1st St"), ("US 1 & SAMPLE CIRCLE", {}, "US 1 & Sample Cir"),
    ("@SAMPLE SCHOOL - FRONT", {"fallback": "Port Chester"}, "@ Sample School, Port Chester"),
    ("23 Sample Ln, Rye Brook", {}, "23 Sample Ln, Rye Brook"),                     # a map label: as it is
    ("Sample Ln between Example Ave and N Example St, Rye Brook", {}, "Sample Ln between Example Ave and N Example St, Rye Brook"),
    ("2323 Sample Lane", {"within": ("Rye Brook", "Port Chester")}, "2323 Sample Ln, Rye Brook"),
    ("330 Samplewerth Place", {"within": ("Rye Brook",)}, "330 Samplewerth Pl"),   # misheard: never snapped, no town guessed
    ("Station On A Drill, Drill Only. 12345", {}, "Station On A Drill, Drill Only. 12345"),   # not an address: as said
    ("", {}, ""),
])
def test_normal_location(raw, kw, want):
    assert normal_location(raw, **kw) == want


def test_town_label():
    t1, t2, t3 = towns()
    assert town_label(snake(t3)) == t3 and town_label(t3.lower()) == t3
    assert town_label("OTHERTOWN") == "Othertown" and town_label("OTHER_PLACE") == "Other Place"
    assert town_label("SAMPLE_V") == "Sample" and town_label("SAMPLE_C") == "Sample" and town_label("SAMPLE_T") == "Town of Sample"
    assert town_label("") == ""


# -- matching reads what it read before -----------------------------------------------------------------------------

CORPUS = ["12 EXAMPLE AVE {s1}", "12 EXAMPLE AVE", "12 EXAMPLE AVENUE APT 3", "12 EXAMPLE AVE : @SAMPLE SCHOOL {s2}",
          "14 EXAMPLE AVE", "12 N EXAMPLE AVE", "12 NORTH EXAMPLE AVENUE", "12 EXAMPLE AVE EXT", "SAMPLE AVE & EXAMPLETON RD",
          "EXAMPLETON RD/SAMPLE AVE OTHERTOWN", "@SAMPLE SCHOOL - FRONT", "12 EXAMPLE AVE {t1}", "12 EXAMPLE AVE, {T2}",
          "7 SAMPLE TER", "SAMPLE TERRACE", "2323 EXAMPLE LN", "23 EXAMPLE LN SAMPLE_V", "23 EXAMPLE LN",
          "12-14 EXAMPLE AVE 3RD FL", "706 SAMPLE CT :3RD FL REAR SAMPLE_V", "I-287 EB MM 9.0", "12 MCSAMPLE PL",
          "5 1ST ST", "12 EXAMPLE AVE ({T2})", "12 EXAMPLE AVE OTHERTOWN"]


def corpus() -> list[str]:
    t1, t2, _ = towns()
    return [c.format(s1=snake(t1), s2=snake(t2), t1=t1.lower(), T2=t2) for c in CORPUS]


def test_a_feeds_raw_text_matches_exactly_as_its_old_title_did():
    """Before: a feed's incident was matched by its title (shown_location of the feed's text). Now: by the feed's text
    itself (address_raw). Every comparison matching makes comes out the same, against raw and normalised others."""
    c = corpus()
    others = c + [normal_location(x, typed=True) for x in c] + [shown_location(x) for x in c]
    for raw in c:
        old = shown_location(raw)
        assert src.street_of(raw) == src.street_of(old) and src._town(raw) == src._town(old), raw
        for o in others:
            assert same_place(o, raw) == same_place(o, old), (o, raw)
            assert same_place_halved(o, raw, True, False) == same_place_halved(o, old, True, False), (o, raw)


def test_matching_reads_the_raw_text_never_the_normalised_one():
    inc = {"address": "12 Example Ave, Rye Brook", "address_raw": "12 EXAMPLE AVE"}
    assert match_address(inc) == "12 EXAMPLE AVE"
    assert match_address({"address": "12 Example Ave", "address_raw": ""}) == "12 Example Ave"     # a person's
    assert match_address({"address": "12 Example Ave"}) == "12 Example Ave"


# -- at ingest -----------------------------------------------------------------------------------------------------


def test_a_feed_incident_stores_the_normal_address_and_its_raw_text(tmp_path):
    _, _, t3 = towns()
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location=f"40 NORTH EXAMPLE STREET {snake(t3)}", units=("E60",),
                 times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert inc["address"] == f"40 N Example St, {t3}" and inc["address_raw"] == f"40 NORTH EXAMPLE STREET {snake(t3)}"
    assert f"at 40 N Example St, {t3}" in inc["remarks"]
    rig.page()                                                     # unchanged matching: the page elsewhere is its own
    assert len(rig.incidents()) == 2


def test_an_out_of_area_intersection_gets_its_town_from_the_feed(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="MVA", location="EXAMPLETON RD/SAMPLE AVE OTHERTOWN", units=("E60",),
                 times={"dispatched": T0}, extra={"town": "Othertown", "jurisdiction": "Rye Brook"})
    rig.poll()
    [inc] = rig.incidents()
    assert inc["address"] == "Exampleton Rd & Sample Ave, Othertown"
    assert inc["address_raw"] == "EXAMPLETON RD/SAMPLE AVE OTHERTOWN"


def test_a_feed_with_no_town_takes_its_jurisdiction(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 OTHER ST", units=("E60",), times={"dispatched": T0},
                 extra={"jurisdiction": "Rye Brook"})
    rig.poll()
    assert rig.incidents()[0]["address"] == "12 Other St, Rye Brook"


def test_a_feed_moving_the_incident_writes_both_and_notes_the_normal_form(tmp_path):
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 SAMPLE LN", units=("E60",), times={"dispatched": T0})
    rig.poll()
    rig.feed.put("A1", type="ALARM/COMM", location="14 SAMPLE LN", units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    assert (inc["address"], inc["address_raw"]) == ("14 Sample Ln, Rye Brook", "14 SAMPLE LN")
    assert "County feed: location 14 Sample Ln, Rye Brook" in rig.feed_lines(inc["id"])


def test_a_page_incident_keeps_the_heard_words_for_matching(tmp_path):
    rig = Rig(tmp_path)
    inc = rig.page()
    assert inc["address"] == "12 Example Ave, Rye Brook" and inc["address_raw"] == "12 Example Avenue"
    rig.feed.put("A1", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60", "L31"), times={"dispatched": T0})
    rig.poll()
    assert len(rig.incidents()) == 1                                # the feed joins it, as before


def test_a_person_editing_the_address_makes_it_theirs(tmp_path):
    rig = Rig(tmp_path)
    inc = rig.page()
    rig.cad.accept(inc["id"], {"type": "ALARM", "subtype": "COMM", "operator": "Lt. Test"})
    rig.cad.update(inc["id"], {"address": "14 Example Ave", "operator": "Lt. Test"})
    got = rig.cad.store.incident(inc["id"])
    assert (got["address"], got["address_raw"]) == ("14 Example Ave", "")     # matched as typed from now on
    assert match_address(got) == "14 Example Ave"


# -- every view reads the stored address ----------------------------------------------------------------------------


def test_the_views_show_the_normal_address(tmp_path):
    _, _, t3 = towns()
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location=f"40 EXAMPLE AVE {snake(t3)}", units=("E60",), times={"dispatched": T0})
    rig.poll()
    want = f"40 Example Ave, {t3}"
    [inc] = rig.incidents()
    st = rig.cad.state()                                           # the API, the list, the call panel, queue cards
    assert [i["address"] for i in st["incidents"]] == [want]
    assert [c["address"] for c in mapview.overview(st["incidents"])] == [want]     # the overview / wallboard
    pinned = dict(inc, lat=41.0, lon=-73.6)
    assert [p["address"] for p in mapview.map_slice([], [pinned], None, [])["incidents"]] == [want]   # map labels
    assert dispatcher.where(inc).startswith("40 Example Avenue")    # the voice
    assert rig.cad.alarms.push_address({"incident_id": inc["id"]}, inc["address"]) == ""   # a feed's: never pushed
    rig.cad.sources.mark_manual(inc["id"], ["location"])
    rig.feed.put("A1", type="ALARM/COMM", location=f"44 EXAMPLE AVE {snake(t3)}", units=("E60",), times={"dispatched": T0})
    rig.poll()
    assert f"County feed: location 44 Example Ave, {t3} (kept the location set by hand)" in rig.feed_lines(inc["id"])
    [dec] = rig.cad.sources.decorate([rig.cad.store.incident(inc["id"])])
    assert dec["feed"]["use"]["location"] == f"44 Example Ave, {t3}"            # "Use the feed's location" hover


# -- the backfill ----------------------------------------------------------------------------------------------------


def old_row(rig, address: str, source: str = "CAD feed", ref: str = "", feed_text: str = "", status: str = "queued") -> int:
    """An incident as the hub stored it before (no address_raw), with the feed's report when feed_text is given."""
    iid = rig.cad.store.create_incident(address=address, source=source, source_ref=ref or None, status="queued")
    with rig.cad.store.tx() as db:
        db.execute("UPDATE incidents SET address_raw = '', status = ? WHERE id = ?", (status, iid))
        if feed_text:
            db.execute("INSERT INTO feed_addresses (incident_id, address) VALUES (?, ?)", (iid, address))
    if feed_text:
        rig.cad.sources._put("county", ref or f"E{iid}", 1, "County feed", T0, {"location": feed_text, "extra": {}},
                             incident_id=iid)
    return iid


def test_the_backfill_normalises_old_rows_once_and_keeps_what_matching_read(tmp_path):
    t1, _, t3 = towns()
    logs: list[str] = []
    rig = Rig(tmp_path)
    rig.cad.sources.log = logs.append
    a = old_row(rig, f"40 EXAMPLE AVE {t3.lower()}", ref="E1", feed_text=f"40 EXAMPLE AVE {snake(t3)}")
    b = old_row(rig, "EXAMPLETON RD/SAMPLE AVE OTHERTOWN", ref="E2", feed_text="EXAMPLETON RD/SAMPLE AVE OTHERTOWN",
                status="closed")
    c = old_row(rig, "12 Sample Lane", source="60 Control page", ref="monitor:p1", status="dismissed")
    hand = old_row(rig, "12 SAMPLE LN", ref="E3", feed_text="12 SAMPLE LN")
    rig.cad.sources.mark_manual(hand, ["location"])
    mine = rig.cad.store.create_incident(type="ALARM", subtype="COMM", address="12 SAMPLE LN", operator="Lt. Test")
    before = {r["id"]: r["address"] for r in rig.incidents()}
    assert rig.cad.sources.backfill_addresses() == 3
    got = {r["id"]: r for r in rig.incidents()}
    assert got[a]["address"] == f"40 Example Ave, {t3}"
    assert got[b]["address"] == "Exampleton Rd & Sample Ave, Othertown"          # closed too
    assert got[c]["address"] == "12 Sample Ln, Rye Brook"                         # dismissed too
    for i in (a, b, c):
        assert got[i]["address_raw"] == before[i]                                 # matching reads what it read
        assert match_address(got[i]) == before[i]
    assert got[hand]["address"] == "12 SAMPLE LN" and got[mine]["address"] == "12 SAMPLE LN"   # a person's: never
    assert rig.cad.sources.feed_address(a, got[a]["address"])                     # still never in a push
    assert logs == ["incident sources: 3 stored addresses normalised"]           # a count, nothing else
    assert rig.cad.sources.backfill_addresses() == 0                              # twice: nothing more
    assert {r["id"]: (r["address"], r["address_raw"]) for r in rig.incidents()} == {
        i: (r["address"], r["address_raw"]) for i, r in got.items()}
    del t1


def test_the_backfill_on_an_empty_database(tmp_path):
    rig = Rig(tmp_path)
    assert rig.cad.sources.backfill_addresses() == 0 and rig.incidents() == []


def test_matching_after_the_backfill_is_as_before(tmp_path):
    """An old feed incident, backfilled: a page for it joins it as it would have before."""
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60", "L31"), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.incidents()
    with rig.cad.store.tx() as db:                                  # as the hub stored it before
        db.execute("UPDATE incidents SET address = '12 EXAMPLE AVE', address_raw = '' WHERE id = ?", (inc["id"],))
    rig.cad.sources.backfill_addresses()
    assert rig.cad.store.incident(inc["id"])["address"] == "12 Example Ave, Rye Brook"
    rig.at(60)
    assert rig.page()["id"] == inc["id"] and len(rig.incidents()) == 1


# -- one call, three incidents (a doubled house number, a pending id) -----------------------------------------------

DOUBLED_NO_UNITS = ("Port Chester Fire Department, respond to 2323 Sample Lane, between Example Avenue and the dead end. "
                    "Time out 1432.")


def placeholder_rig(tmp_path, monkeypatch):
    import tests.test_dept_placeholders as dp
    import tests.test_incident_sources as tis
    from tests.test_cad import make_cad
    path = dp.lists_file(tmp_path)
    monkeypatch.setattr(tis, "make_cad", lambda tmp, clock=None: make_cad(tmp, clock=clock, lists=path))
    return Rig(tmp_path)


def test_a_doubled_number_page_with_no_units_and_the_feeds_placeholder_event_are_one_call(tmp_path, monkeypatch):
    """The page heard the house number doubled and named no unit or type; the county's event came 30 s later naming
    only department placeholders, with its town token. One incident, whatever the addresses look like."""
    _, _, t3 = towns()
    rig = placeholder_rig(tmp_path, monkeypatch)
    p = rig.page("p1", DOUBLED_NO_UNITS)
    rig.at(50)
    rig.feed.put("F0000001", type="ALARM/RESD", location=f"23 SAMPLE LN {snake(t3)}", units=("DPTA", "DPTB"),
                 times={"dispatched": T0 + 20}, extra={"town": t3, "jurisdiction": t3})
    rig.poll()
    assert [i["id"] for i in open_incidents(rig)] == [p["id"]]
    rig.at(120)                                                    # the same event, its run numbers and units now
    rig.feed.put("F0000001", type="ALARM/RESD", location=f"23 SAMPLE LN {snake(t3)}", units=("DPTA", "DPTB", "E60"),
                 times={"dispatched": T0 + 20, "responding": T0 + 60}, extra={"town": t3, "jurisdiction": t3})
    rig.poll()
    [inc] = open_incidents(rig)
    assert inc["id"] == p["id"] and inc["address"] == f"23 Sample Ln, {t3}"


def test_the_same_call_under_a_second_event_id_is_never_silent(tmp_path, monkeypatch):
    """Before the adapter kept a pending listing's key, a call could come again under a new id ("...-2"). The hub never
    joins two events of one source, but it notes them as possibly the same."""
    _, _, t3 = towns()
    rig = placeholder_rig(tmp_path, monkeypatch)
    rig.feed.put("F0000001", type="ALARM/RESD", location=f"23 SAMPLE LN {snake(t3)}", units=("DPTA",),
                 times={"dispatched": T0})
    rig.poll()
    rig.at(60)
    rig.feed.put("F0000001-2", type="ALARM/RESD", location=f"23 SAMPLE LN {snake(t3)}", units=("DPTA", "E60"),
                 times={"dispatched": T0})
    rig.poll()
    a, b = [i["id"] for i in rig.incidents()]
    assert possibly(rig, b, a)
