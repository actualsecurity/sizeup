"""An incident's address is stored normalised (sources.normal_location) and the text it came from is kept beside it
(incidents.address_raw) for matching and audit (sources.match_address). Every view reads the stored address; matching
reads exactly what it read before. Old rows are backfilled once (Sources.backfill_addresses). Synthetic data only:
made-up streets, towns from the data file's own list, made-up event ids and times."""

from __future__ import annotations

import pytest

from sizeup.cad import dispatcher, mapview
from sizeup.cad import sources as src
from sizeup.cad.sources import checked_location, match_address, normal_location, same_place, same_place_halved, shown_location, town_label
from tests.test_incident_sources import T0, Rig, towns
from tests.test_street_only import open_incidents

STREETS = {"rye brook": ["Example Ave", "Sample Ln", "DeSample Rd", "N Example St"],
           "port chester": ["Exampleton Rd", "Sample Ave", "Specimen St", "North St"],
           "othertown": ["Distant Way"]}                  # a neighbouring town of the street lists (out of area)


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
    ("12 SAMPLE CT :3RD FL REAR OTHER_V", {"typed": True}, "12 Sample Ct 3rd Fl Rear, Other"),   # a village's token; the floor kept
    ("12 SAMPLE DR OTHER_T", {"typed": True}, "12 Sample Dr, Town of Other"),
    ("12 SAMPLE AVE : @SAMPLE SCHOOL - FRONT (E1) OTHER_V", {"typed": True}, "12 Sample Ave (Sample School), Other"),
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
    assert f"at 40 North Example Street {t3}" in inc["remarks"]     # the remarks: the feed's whole text, as before
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
    assert inc["address"] == "12 Example Ave" and inc["address_raw"] == "12 Example Avenue"   # unconfirmed: no town guessed
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


KEY_CHECK_0 = ("incident sources: address backfill: 0 premise notes / pre-plan rows whose address key would change "
               "(0 keyed rows checked)")


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
    assert got[c]["address"] == "12 Sample Ln"                                    # dismissed too (unplaced: no guess)
    for i in (a, b, c):
        assert got[i]["address_raw"] == before[i]                                 # matching reads what it read
        assert match_address(got[i]) == before[i]
    assert got[hand]["address"] == "12 SAMPLE LN" and got[mine]["address"] == "12 SAMPLE LN"   # a person's: never
    assert rig.cad.sources.feed_address(a, got[a]["address"])                     # still never in a push
    assert logs == [KEY_CHECK_0, "incident sources: 3 stored addresses normalised"]   # counts, nothing else
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
    """Before the adapter kept a pending listing's key, a call could come again under a new id ("...-2"). One county
    event, one incident: the second id is another run of it (sources.py "same event"), said on the timeline."""
    _, _, t3 = towns()
    rig = placeholder_rig(tmp_path, monkeypatch)
    rig.feed.put("F0000001", type="ALARM/RESD", location=f"23 SAMPLE LN {snake(t3)}", units=("DPTA",),
                 times={"dispatched": T0})
    rig.poll()
    rig.at(60)
    rig.feed.put("F0000001-2", type="ALARM/RESD", location=f"23 SAMPLE LN {snake(t3)}", units=("DPTA", "E60"),
                 times={"dispatched": T0})
    rig.poll()
    [a] = [i["id"] for i in rig.incidents()]
    assert any(e["text"].startswith("Redispatched") for e in rig.cad.store.q(
        "SELECT text FROM events WHERE incident_id = ?", (a,)))


# -- review of #475 ---------------------------------------------------------------------------------------------------


def joint_rig(tmp_path):
    """A rig whose lists make Rye Brook a joint-response town (their units first on the run card)."""
    rig = Rig(tmp_path)
    assert "Rye Brook" in rig.cad.lists.joint_response
    return rig


def test_a_jurisdiction_only_town_is_shown_but_never_makes_a_joint_call(tmp_path):
    """The dispatch group's jurisdiction fills the shown town; it never says where the call is (as on main)."""
    rig = joint_rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 OTHER ST", units=("E60",), times={"dispatched": T0},
                 extra={"jurisdiction": "Rye Brook"})
    rig.poll()
    [inc] = rig.cad.state()["incidents"]
    assert inc["address"] == "12 Other St, Rye Brook"
    assert inc["town"] == "" and inc["joint_call"] == ""


def test_a_named_town_still_makes_the_joint_call(tmp_path):
    rig = joint_rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 OTHER ST RYE_BROOK", units=("E60",), times={"dispatched": T0})
    rig.poll()
    [inc] = rig.cad.state()["incidents"]
    assert inc["joint_call"] == "Rye Brook"


def test_an_unconfirmed_page_gets_no_street_list_town_and_no_joint_call(tmp_path):
    rig = joint_rig(tmp_path)
    page = ("Port Chester Fire Department, Engine 60, respond to an activated fire alarm. 12 Sample Lane. Time out 1432.")
    p = rig.page("p1", page)
    assert p["address"] == "12 Sample Ln" and p["address_raw"] == "12 Sample Lane"   # Sample Ln: a Rye Brook street
    [inc] = rig.cad.state()["incidents"]
    assert inc["town"] == "" and inc["joint_call"] == ""


@pytest.mark.parametrize("raw,want", [
    ("12 EXAMPLE AVE REAR", "12 Example Ave Rear, Rye Brook"),     # the town: its street list
    ("12 EXAMPLE AVE REAR OTHERTOWN", "12 Example Ave Rear Othertown, Rye Brook"),
    ("12 EXAMPLE AVE BASEMENT", "12 Example Ave Basement, Rye Brook"),
    ("SAMPLE PKWY NB", "Sample Pkwy NB"),
    ("12 SAMPLE RD OTHERTOWN CT", "12 Sample Rd, Othertown"),       # the state code dropped, the town kept
    ("12 OTHER RD CT", "12 Other Rd Ct"),
    ("12 ST SAMPLES", "12 St Samples"),                             # a saint's street: no town
])
def test_words_after_the_suffix_are_a_town_only_when_one_plain_word(raw, want):
    assert normal_location(raw, typed=True) == want


def test_a_unit_word_after_the_street_stays_a_unit_for_the_address_history():
    from sizeup.cad import addrkey
    p = addrkey.parse(normal_location("12 EXAMPLE AVE REAR", typed=True), {})
    assert p.unit == "rear" and p.number == "12"


def test_a_town_hint_is_never_stripped_down_to_a_nameless_street():
    assert normal_location("12 OTHERTOWN", town="Othertown", typed=True) == "12 Othertown, Othertown"
    assert normal_location("12 SAMPLE AVE OTHERTOWN", town="Othertown", typed=True) == "12 Sample Ave, Othertown"


def test_a_reading_that_is_not_the_same_place_is_never_stored(tmp_path, monkeypatch):
    """The ingest guard: a normal form that isn't same_place as the feed's text falls back to shown_location."""
    monkeypatch.setattr(src, "normal_location", lambda *a, **k: "99 Elsewhere Rd")
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location="12 EXAMPLE AVE", units=("E60",), times={"dispatched": T0})
    rig.poll()
    assert rig.incidents()[0]["address"] == shown_location("12 EXAMPLE AVE") == "12 Example Ave"


def test_the_backfill_is_on_its_own_thread_and_never_at_start(tmp_path, monkeypatch):
    """start() returns at once and nothing of the backfill runs for BACKFILL_DELAY_S (minutes): a takeover's start
    writes none of it. After the delay it runs on a thread of its own, never the ticker's."""
    import threading
    assert src.BACKFILL_DELAY_S >= 120
    rig = Rig(tmp_path)
    iid = old_row(rig, "12 EXAMPLE AVE", ref="E1", feed_text="12 EXAMPLE AVE")
    rig.cad.sources.start()                                        # the real delay: still waiting when stopped
    assert {t.name for t in threading.enumerate()} >= {"incident-sources", "address-backfill"}
    threading.Event().wait(0.2)
    rig.cad.sources.stop(5)
    assert rig.cad.store.incident(iid)["address_raw"] == ""        # untouched
    assert not rig.cad.sources._backfill_thread.is_alive()
    rig = Rig(tmp_path / "second")
    iid = old_row(rig, "12 EXAMPLE AVE", ref="E1", feed_text="12 EXAMPLE AVE")
    where: list[str] = []
    one = rig.cad.sources._backfill_one
    rig.cad.sources._backfill_one = lambda inc: (where.append(threading.current_thread().name), one(inc))[1]
    monkeypatch.setattr(src, "BACKFILL_PAUSE_S", 0.0)
    rig.cad.sources.start(backfill_delay_s=0.0)
    rig.cad.sources._backfill_thread.join(10)
    assert where == ["address-backfill"] and rig.cad.store.incident(iid)["address"] == "12 Example Ave, Rye Brook"
    rig.cad.sources.stop(5)


def many_old_rows(rig, n: int) -> list[int]:
    return [old_row(rig, f"{10 + 2 * k} EXAMPLE AVE", ref=f"E{k}", feed_text=f"{10 + 2 * k} EXAMPLE AVE") for k in range(n)]


def todo(rig) -> int:
    return rig.cad.store.one("SELECT count(*) AS n FROM incidents WHERE address_raw = ''")["n"]


def test_the_backfill_goes_in_small_batches_with_the_store_free_between_rows(tmp_path, monkeypatch):
    """BACKFILL_BATCH rows per read, a pause between batches, each row its own transaction: the store's lock is free
    whenever a row starts, and a pass cut short by stop() leaves the rest for the next one."""
    monkeypatch.setattr(src, "BACKFILL_BATCH", 4)
    rig = Rig(tmp_path)
    ids = many_old_rows(rig, 10)
    sources = rig.cad.sources
    reads: list[int] = []
    q = rig.cad.store.q

    def counted(sql, args=()):
        rows = q(sql, args)
        if "FROM incidents i WHERE i.address_raw = ''" in sql:
            reads.append(len(rows))
        return rows
    monkeypatch.setattr(rig.cad.store, "q", counted)
    free: list[bool] = []
    one = sources._backfill_one

    def watched(inc):
        got = rig.cad.store.lock.acquire(blocking=False)           # an RLock: ours only if no one else holds it
        if got:
            rig.cad.store.lock.release()
        free.append(got and not rig.cad.store.db.in_transaction)
        return one(inc)
    sources._backfill_one = watched
    pauses: list[float] = []
    wait = sources._stop.wait
    monkeypatch.setattr(sources._stop, "wait", lambda t=None: (pauses.append(t), len(pauses) >= 2)[1])   # stop at the 2nd
    assert sources.backfill_addresses(pause=0.5) == 8
    assert reads == [4, 4] and pauses == [0.5, 0.5] and all(free) and todo(rig) == 2
    monkeypatch.setattr(sources._stop, "wait", wait)
    assert sources.backfill_addresses(pause=0.0) == 2 and todo(rig) == 0          # the next pass: only what was left
    assert reads == [4, 4, 2]
    assert all(rig.cad.store.incident(i)["address"].endswith("Example Ave, Rye Brook") for i in ids)


def test_a_fenced_backfill_stops_and_goes_on_when_the_leader_again(tmp_path, monkeypatch):
    """The lease runs out mid-pass (#509): the write is refused, nothing of that row is kept, the thread stops (said
    once) and writes nothing more while it isn't the leader; the leader again, it does the rows still to do."""
    import threading
    from sizeup.fence import FencedWrite
    monkeypatch.setattr(src, "BACKFILL_BATCH", 3)
    monkeypatch.setattr(src, "BACKFILL_PAUSE_S", 0.0)
    logs: list[str] = []
    rig = Rig(tmp_path)
    sources = rig.cad.sources
    sources.log = logs.append
    ids = many_old_rows(rig, 8)
    leader = [True]
    rig.cad.may_write = lambda: leader[0]
    done: list[int] = []
    fenced, tried = threading.Event(), threading.Event()
    one = sources._backfill_one

    def lease_runs_out(inc):
        if len(done) == 4 and leader[0] and not fenced.is_set():
            leader[0] = False                                       # ... as this row is being written
            fenced.set()
        elif fenced.is_set():
            tried.set()
        changed = one(inc)
        done.append(inc["id"])
        return changed
    sources._backfill_one = lease_runs_out
    with pytest.raises(FencedWrite):
        with rig.cad.store.tx() as db:                              # the guard itself, as the App wires it
            leader[0] = False
            db.execute("UPDATE incidents SET address_raw = 'x' WHERE id = ?", (ids[0],))
    leader[0] = True
    assert todo(rig) == 8
    t = threading.Thread(target=sources.backfill_run, args=(0.0, 0.02), daemon=True)
    t.start()
    assert fenced.wait(10)
    for _ in range(500):
        if any("stopped until" in x for x in logs):
            break
        threading.Event().wait(0.01)
    threading.Event().wait(0.2)                                     # several retries meanwhile: all refused before a row
    assert t.is_alive() and not tried.is_set()
    assert len(done) == 4 and todo(rig) == 4                        # the refused row and the rest: as they were
    assert logs.count("incident sources: address backfill: stopped until this is the leader again") == 1
    assert "incident sources: 4 stored addresses normalised" in logs
    with pytest.raises(FencedWrite):
        sources.backfill_addresses()                                # a direct call says so too, nothing written
    assert todo(rig) == 4
    leader[0] = True                                                # the leader again
    t.join(10)
    assert not t.is_alive() and todo(rig) == 0 and sorted(done) == sorted(ids)   # each row once
    assert all(rig.cad.store.incident(i)["address"].endswith("Example Ave, Rye Brook") for i in ids)
    assert logs.count("incident sources: 4 stored addresses normalised") == 2


def test_the_backfill_does_open_incidents_first_and_writes_only_changes(tmp_path):
    rig = Rig(tmp_path)
    closed = old_row(rig, "12 EXAMPLE AVE", ref="E1", feed_text="12 EXAMPLE AVE", status="closed")
    opened = old_row(rig, "14 EXAMPLE AVE", ref="E2", feed_text="14 EXAMPLE AVE")
    same = old_row(rig, "16 Example Ave, Rye Brook", ref="E3", feed_text="16 EXAMPLE AVE RYE_BROOK")
    order: list[int] = []
    one = rig.cad.sources._backfill_one
    rig.cad.sources._backfill_one = lambda inc: (order.append(inc["id"]), one(inc))[1]
    rig.cad.store.db.execute("DELETE FROM addr_queue")
    assert rig.cad.sources.backfill_addresses() == 2
    assert order[-1] == closed and set(order) == {opened, closed, same}      # the open ones first
    queued = {r[0] for r in rig.cad.store.db.execute("SELECT id FROM addr_queue")}
    assert same not in queued and {opened, closed} <= queued        # unchanged: no address-history churn
    assert rig.cad.store.incident(same)["address_raw"] == "16 Example Ave, Rye Brook"   # done all the same


def test_one_bad_row_never_stops_the_backfill_nor_comes_back(tmp_path):
    logs: list[str] = []
    rig = Rig(tmp_path)
    rig.cad.sources.log = logs.append
    bad = old_row(rig, "12 EXAMPLE AVE", ref="E1", feed_text="12 EXAMPLE AVE")
    good = old_row(rig, "14 EXAMPLE AVE", ref="E2", feed_text="14 EXAMPLE AVE")
    one = rig.cad.sources._backfill_one

    def flaky(inc):
        if inc["id"] == bad:
            raise ValueError("a row the code can't read")
        return one(inc)
    rig.cad.sources._backfill_one = flaky
    assert rig.cad.sources.backfill_addresses() == 1
    assert rig.cad.store.incident(good)["address"] == "14 Example Ave, Rye Brook"
    b = rig.cad.store.incident(bad)
    assert (b["address"], b["address_raw"]) == ("12 EXAMPLE AVE", "12 EXAMPLE AVE")   # marked: never retried
    assert logs == [KEY_CHECK_0, "incident sources: 1 stored addresses normalised, 1 left as they were (failed)"]
    assert rig.cad.sources.backfill_addresses() == 0


def test_a_feed_made_incident_keeps_its_push_guard_even_without_the_old_record(tmp_path):
    rig = Rig(tmp_path)
    iid = old_row(rig, "12 EXAMPLE AVE", ref="E1")                 # a feed's incident, its feed_addresses row missing
    rig.cad.sources.backfill_addresses()
    inc = rig.cad.store.incident(iid)
    assert inc["address"] != "12 EXAMPLE AVE" and rig.cad.sources.feed_address(iid, inc["address"])


# -- a town only when it is one we know ------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,want", [
    ("12 SAMPLE AVE MAIN", "12 Sample Ave Main, Port Chester"),
    ("12 SAMPLE AVE CLUBHOUSE", "12 Sample Ave Clubhouse, Port Chester"),
    ("12 SAMPLE AVE TRLR", "12 Sample Ave Trlr, Port Chester"),
    ("12 SAMPLE AVE SOUTHSIDE", "12 Sample Ave Southside, Port Chester"),
    ("12 OTHER AVE UNKNOWNVILLE", "12 Other Ave Unknownville"),          # not a town this CAD knows: stays
    ("12 OTHER AVE OTHERTOWN", "12 Other Ave, Othertown"),              # a town of the street lists: its town
    ("12 OTHER AVE OTHERTOWN CT", "12 Other Ave, Othertown"),
])
def test_a_trailing_word_is_a_town_only_when_known(raw, want):
    assert normal_location(raw, typed=True) == want


def test_a_feeds_own_town_is_shown_as_given_and_never_replaced_by_a_local_one():
    """The feed names a town this CAD doesn't know: shown as given, in proper case, its state dropped, and nothing is
    guessed over it: not the street lists' town, not the jurisdiction. A word that is never a town isn't one."""
    assert not src.known_town("Farawayton") and "specimen st" in {n.lower() for n in STREETS["port chester"]}
    kw = {"typed": True, "fallback": "Port Chester"}
    assert normal_location("12 OTHER RD", town="FARAWAYTON", **kw) == "12 Other Rd, Farawayton"
    assert normal_location("12 SPECIMEN ST", town="FARAWAYTON", **kw) == "12 Specimen St, Farawayton"   # a local street's name
    assert normal_location("12 OTHER RD", town="FARAWAYTON CT", **kw) == "12 Other Rd, Farawayton"       # a trailing state
    assert normal_location("12 OTHER RD FARAWAYTON CT", town="FARAWAYTON CT", **kw) == "12 Other Rd, Farawayton"
    assert normal_location("12 OTHER RD FARAWAYTON", town="Farawayton, CT", **kw) == "12 Other Rd, Farawayton"
    assert normal_location("12 OTHER RD", town="FAR_AWAY_TON", **kw) == "12 Other Rd, Far Away Ton"
    assert checked_location("12 OTHER RD", normal_location("12 OTHER RD", town="FARAWAYTON", **kw)) == "12 Other Rd, Farawayton"
    # no town given, or none that can be one: the guesses as before
    assert normal_location("12 OTHER RD", town="", **kw) == "12 Other Rd, Port Chester"
    assert normal_location("12 OTHER RD", town="UNKNOWN", **kw) == "12 Other Rd, Port Chester"
    assert normal_location("12 OTHER RD", town="N/A", **kw) == "12 Other Rd, Port Chester"
    assert normal_location("12 OTHER ST BASEMENT", town="Basement", typed=True) == "12 Other St Basement"
    assert normal_location("12 OTHER ST OTHERTOWN", town="Othertown", typed=True) == "12 Other St, Othertown"
    assert normal_location("12 OTHER ST", town="OTHERTOWN_T", typed=True) == "12 Other St, Town of Othertown"


def test_known_towns():
    try:
        assert src.known_town("Rye Brook") and src.known_town("RYE_BROOK") and src.known_town("othertown")
        assert src.known_town("Town of Othertown") and src.known_town("OTHERTOWN_V")
        assert not src.known_town("Unknownville") and not src.known_town("Basement") and not src.known_town("")
        assert not src.known_town("Jointville")
        src.set_known_towns(["Jointville"])                             # a joint-response town
        assert src.known_town("JOINTVILLE") and normal_location("12 OTHER AVE JOINTVILLE", typed=True) == \
            "12 Other Ave, Jointville"
    finally:
        src.set_known_towns(())


def test_a_persons_edit_during_the_backfill_is_never_overwritten(tmp_path):
    """The row read, then a person edits its address before the backfill writes: their address stands, address_raw
    stays empty (theirs), on the unchanged path and the failure path alike."""
    rig = Rig(tmp_path)
    same = old_row(rig, "16 Example Ave, Rye Brook", ref="E1", feed_text="16 EXAMPLE AVE RYE_BROOK")   # unchanged path
    bad = old_row(rig, "12 EXAMPLE AVE", ref="E2", feed_text="12 EXAMPLE AVE")                         # failure path
    one = rig.cad.sources._backfill_one

    def edited_meanwhile(inc):
        with rig.cad.store.tx() as db:
            db.execute("UPDATE incidents SET address = '20 Example Ave' WHERE id = ?", (inc["id"],))
        if inc["id"] == bad:
            raise ValueError("fails after the edit")
        return one(inc)
    rig.cad.sources._backfill_one = edited_meanwhile
    rig.cad.sources.backfill_addresses()
    for iid in (same, bad):
        got = rig.cad.store.incident(iid)
        assert (got["address"], got["address_raw"]) == ("20 Example Ave", "")


# -- the county's towns (data/westchester_towns.txt) -------------------------------------------------------------------


@pytest.mark.parametrize("raw,want", [
    ("EXAMPLETON RD/SAMPLE AVE BEDFORD", "Exampleton Rd & Sample Ave, Bedford"),          # a farther town of the county
    ("12 SAMPLE CT SOMERS", "12 Sample Ct, Somers"),
    ("12 SAMPLE CT VALHALLA", "12 Sample Ct, Valhalla"),                                  # a postal hamlet
    ("12 SAMPLE CT GREENWICH CT", "12 Sample Ct, Greenwich"),                             # the neighbour across the line
    ("12 SAMPLE AVE MAIN", "12 Sample Ave Main, Port Chester"),                           # still not towns
    ("12 SAMPLE AVE CLUBHOUSE", "12 Sample Ave Clubhouse, Port Chester"),
])
def test_any_county_town_typed_last_is_its_town(raw, want):
    assert normal_location(raw, typed=True) == want


def test_county_town_spellings_and_town_of_forms():
    assert normal_location("12 SAMPLE CT CROTON_ON_HUDSON", typed=True) == "12 Sample Ct, Croton-on-Hudson"
    assert normal_location("12 SAMPLE CT", town="Town of Somers", typed=True) == "12 Sample Ct, Town of Somers"
    assert normal_location("12 SAMPLE CT", town="SOMERS_T", typed=True) == "12 Sample Ct, Town of Somers"
    assert normal_location("12 SAMPLE CT", town="NEW_CASTLE", typed=True) == "12 Sample Ct, New Castle"
    assert src.known_town("Town of Bedford") and src.known_town("MOUNT_KISCO_V") and not src.known_town("Town of Main")
    assert len([k for k in src.county_towns() if k]) >= 80


# -- the final review: units and places kept, fractions, names, the address keys -------------------------------------


@pytest.mark.parametrize("raw,kw,want", [
    ("12 EXAMPLE AVE :APT 3B REAR RYE_BROOK", {"typed": True}, "12 Example Ave Apt 3B Rear, Rye Brook"),
    ("12 EXAMPLE AVE : @SAMPLE DINER RYE_BROOK", {"typed": True}, "12 Example Ave (Sample Diner), Rye Brook"),
    ("12 EXAMPLE AVE APT 3B : @SAMPLE DINER", {"typed": True}, "12 Example Ave Apt 3B (Sample Diner), Rye Brook"),
    ("12 EXAMPLE AVE #2B RYE_BROOK", {"typed": True}, "12 Example Ave #2B, Rye Brook"),
    ("12-14 EXAMPLE AVE 3RD FL", {"typed": True}, "12 Example Ave 3rd Fl, Rye Brook"),
    ("12 EXAMPLE AVE :REAR", {"typed": True}, "12 Example Ave Rear, Rye Brook"),
    ("12 EXAMPLE AVE APT 3 rye brook", {"typed": True}, "12 Example Ave Apt 3, Rye Brook"),        # the town isn't the unit's
    ("12 Sample Lane apartment 3", {}, "12 Sample Ln Apt 3, Rye Brook"),                           # heard
    ("12 Sample Lane building is fully involved with heavy fire", {}, "12 Sample Ln, Rye Brook"),  # heard: not a unit
    ("@SAMPLE SCHOOL (12 EXAMPLE AVE)", {"typed": True}, "12 Example Ave, Rye Brook"),             # as before
    ("12 1/2 EXAMPLE AVE", {"typed": True}, "12 1/2 Example Ave, Rye Brook"),                      # a fraction: one house
    ("12 1/2 OTHER RD", {}, "12 1/2 Other Rd"),
    ("12 1/2 EXAMPLE AVE & SAMPLE LN", {"typed": True}, "12 1/2 Example Ave & Sample Ln, Rye Brook"),
    ("EXAMPLE AVE/SAMPLE LN", {"typed": True}, "Example Ave & Sample Ln, Rye Brook"),              # a slash is still two streets
    ("12 THE CIRCLE", {"typed": True}, "12 The Circle"),                                           # the name, never "The Cir"
    ("12 THE TERRACE OTHERTOWN", {"typed": True}, "12 The Terrace, Othertown"),
    ("12 SAMPLE CIRCLE", {"typed": True}, "12 Sample Cir"),                                        # a suffix after a name
    ("12 CIRCLE DRIVE", {"typed": True}, "12 Circle Dr"),
    ("12 Example Avenue in Rye Brook", {}, "12 Example Ave, Rye Brook"),                           # heard: no stray "In"
    ("12 Example Avenue in the village of Rye Brook", {}, "12 Example Ave, Rye Brook"),
    ("12 INN RD rye brook", {}, "12 Inn Rd, Rye Brook"),
])
def test_units_places_fractions_and_names(raw, kw, want):
    got = normal_location(raw, **kw)
    assert got == want and checked_location(raw, got) == want


def test_a_direction_that_a_known_street_is_written_with_stays_a_word():
    """"East End Ave" is on the street list (in one town): the feed's event in another town, or a second town that
    writes it the same, still reads "East End Ave"; a direction before a street not written so is abbreviated."""
    old = src.STREETS
    try:
        src.set_streets({**STREETS, "rye brook": [*STREETS["rye brook"], "East End Ave"]})
        kw = {"typed": True}
        assert normal_location("12 EAST END AVE", **kw) == "12 East End Ave, Rye Brook"
        assert normal_location("12 EAST END AVE", town="PORT_CHESTER", **kw) == "12 East End Ave, Port Chester"
        assert normal_location("12 EAST END AVE", town="FARAWAYTON", **kw) == "12 East End Ave, Farawayton"
        assert normal_location("12 E END AVE", town="PORT_CHESTER", **kw) == "12 E End Ave, Port Chester"   # as typed
        assert normal_location("12 EAST OTHER AVE", town="PORT_CHESTER", **kw) == "12 E Other Ave, Port Chester"
        assert normal_location("12 NORTH EXAMPLE STREET RYE_BROOK") == "12 N Example St, Rye Brook"   # written "N Example St"
    finally:
        src.STREETS = old


def test_a_changed_house_number_is_never_stored():
    """checked_location: the same street with another number isn't the same reading."""
    assert checked_location("12 1/2 EXAMPLE AVE", "12 1 & 2 Example Ave") == "12 1/2 Example Ave"
    assert checked_location("12 1/2 EXAMPLE AVE", "12 Example Ave") == "12 1/2 Example Ave"
    assert checked_location("12A EXAMPLE AVE", "12 Example Ave") == "12A Example Ave"
    assert checked_location("12 EXAMPLE AVE", "12 Example Ave, Rye Brook") == "12 Example Ave, Rye Brook"
    assert checked_location("12-14 EXAMPLE AVE 3RD FL", "12 Example Ave 3rd Fl, Rye Brook") == "12 Example Ave 3rd Fl, Rye Brook"
    assert checked_location("5 1ST ST", "5 1st St") == "5 1st St" and checked_location("US 1 & SAMPLE CIRCLE", "US 1 & Sample Cir") == "US 1 & Sample Cir"


def test_a_feeds_apartment_floor_and_place_are_shown_everywhere_and_kept_in_the_remarks(tmp_path):
    """The reviewer's repro: ":APT 3B REAR" and ": @SAMPLE DINER" were on no screen. Now the address every view reads
    has them, and the remarks keep the feed's whole text as they did before addresses were normalised."""
    _, _, t3 = towns()
    rig = Rig(tmp_path)
    rig.feed.put("A1", type="ALARM/COMM", location=f"12 EXAMPLE AVE :APT 3B REAR {snake(t3)}", units=("E60",),
                 times={"dispatched": T0})
    rig.feed.put("B1", type="EMS", location=f"14 EXAMPLE AVE : @SAMPLE DINER {snake(t3)}", units=("E62",),
                 times={"dispatched": T0})
    rig.poll()
    a, b = rig.incidents()
    assert a["address"] == f"12 Example Ave Apt 3B Rear, {t3}" and b["address"] == f"14 Example Ave (Sample Diner), {t3}"
    assert "Apt 3B Rear" in a["remarks"] and "Sample Diner" in b["remarks"]
    assert a["address_raw"] == f"12 EXAMPLE AVE :APT 3B REAR {snake(t3)}"
    st = rig.cad.state()                                           # the API: the list, the call panel, queue cards
    shown = {i["id"]: i["address"] for i in st["incidents"]}
    assert "Apt 3B Rear" in shown[a["id"]] and "(Sample Diner)" in shown[b["id"]]
    cards = {c["id"]: c["address"] for c in mapview.overview(st["incidents"])}                    # overview / wallboard
    assert "Apt 3B Rear" in cards[a["id"]] and "(Sample Diner)" in cards[b["id"]]
    pins = mapview.map_slice([], [dict(a, lat=41.0, lon=-73.6), dict(b, lat=41.0, lon=-73.6)], None, [])["incidents"]
    assert ["Apt 3B Rear" in pins[0]["address"], "(Sample Diner)" in pins[1]["address"]] == [True, True]   # map labels
    assert dispatcher.where(a).startswith("12 Example Avenue") and "Sample Diner" in dispatcher.where(b)   # the voice
    # a push never carries a feed's address at all (#297): with the unit and place it still doesn't
    assert rig.cad.alarms.push_address({"incident_id": a["id"]}, a["address"]) == ""
    assert rig.cad.alarms.push_address({"incident_id": b["id"]}, b["address"]) == ""
    # matching is as before: a page for the building joins it, whatever the unit
    rig.feed.put("A1", type="ALARM/COMM", location=f"12 EXAMPLE AVE :APT 4C {snake(t3)}", units=("E60",),
                 times={"dispatched": T0})
    rig.poll()
    assert rig.cad.store.incident(a["id"])["address"] == f"12 Example Ave Apt 3B Rear, {t3}"      # the same place: not moved
    assert f"County feed: location 12 Example Ave Apt 4C, {t3}" in rig.feed_lines(a["id"])        # ... but said


KEY_SAMPLE = [  # (the address as stored before, its source, the feed's text when a feed wrote it)
    ("12 Example Ave", "CAD feed", "12 EXAMPLE AVE :APT 3B REAR"),
    ("14 Example Ave, Rye Brook", "CAD feed", "14 EXAMPLE AVE RYE_BROOK"),
    ("16 Example Ave", "CAD feed", "16 EXAMPLE AVE : @SAMPLE DINER"),
    ("18 Other Rd", "CAD feed", "18 OTHER RD"),                              # unplaced: normalising adds a town
    ("12 Specimen St", "CAD feed", "12 SPECIMEN STREET"),
    ("12 Sample Ln", "CAD feed", ""),                                       # its feed report gone
    ("Exampleton Rd & Sample Ave", "CAD feed", "EXAMPLETON RD/SAMPLE AVE"),
    ("@ Sample School", "CAD feed", "@SAMPLE SCHOOL - FRONT"),
    ("12 1/2 Example Ave", "CAD feed", "12 1/2 EXAMPLE AVE"),
    ("12 The Circle", "CAD feed", "12 THE CIRCLE"),
    ("12 Sample Lane", "60 Control page", ""),
    ("12 Sample Lane apartment 3", "60 Control page", ""),
    ("40 North Example Street in Rye Brook", "60 Control page", ""),
    ("12 Other Road", "60 Control page", ""),
    ("23 Sample Ln, Rye Brook", "60 Control page", ""),
    ("12A EXAMPLE AVE REAR", "60 Control page", ""),
]


def keyed(rig) -> dict:
    rig.cad.addrs.catch_up(2000, transcripts=False)
    return {r["id"]: tuple(r[c] for c in ("addr_key", "bldg_key", "addr_street", "addr_how", "addr_label"))
            for r in rig.cad.store.q("SELECT * FROM incidents")}


def test_no_address_key_changes_when_old_rows_are_normalised(tmp_path):
    """Premise notes, pre-plan points and documents are stored under an incident's building key. The keys are made from
    the form the address had before (key_address), so the backfill changes none: every row of a sample, placed or not,
    has exactly the keys it had; the read-only check before the backfill counts 0 and says so."""
    logs: list[str] = []
    rig = Rig(tmp_path)
    rig.cad.sources.log = logs.append
    ids = [old_row(rig, addr, source=source, ref=f"E{k}", feed_text=text) for k, (addr, source, text) in enumerate(KEY_SAMPLE)]
    before = keyed(rig)
    assert sum(1 for i in ids if before[i][1]) >= 12                             # most have a building key
    now = rig.clock()
    with rig.cad.store.tx() as db:                                               # something stored under every key
        for i in ids:
            if before[i][1]:
                db.execute("INSERT INTO premise_notes (bldg_key, addr_key, label, category, text, created_at, updated_at) "
                           "VALUES (?, ?, ?, 'hazard', 'made-up note', ?, ?)", (before[i][1], before[i][0], before[i][4], now, now))
    noted = len(rig.cad.store.q("SELECT 1 FROM premise_notes"))
    old_addresses = {i: rig.cad.store.incident(i)["address"] for i in ids}
    changed = rig.cad.sources.backfill_addresses()
    assert changed >= 8                                                          # the addresses did change ...
    assert sum(1 for i in ids if rig.cad.store.incident(i)["address"] != old_addresses[i]) == changed
    assert keyed(rig) == before                                                  # ... and not one key
    assert logs[0] == (f"incident sources: address backfill: 0 premise notes / pre-plan rows whose address key would "
                       f"change ({noted} keyed rows checked)")
    for i in ids:                                                                # every note still found from its incident
        if before[i][1]:
            assert rig.cad.store.q("SELECT 1 FROM premise_notes WHERE bldg_key = ?", (rig.cad.store.incident(i)["bldg_key"],))


def test_a_new_feed_or_page_incident_is_keyed_as_it_was_before_normalising(tmp_path):
    """A new incident's keys come from the same inputs as before: a feed's text as shown_location wrote it, a page's
    heard words. The unit, the place and a guessed town in the stored address change nothing."""
    from sizeup.cad import addrkey
    rig = Rig(tmp_path)
    L = rig.cad.lists
    texts = [t for _a, s, t in KEY_SAMPLE if t] + ["12 OTHER RD FARAWAYTON CT", "12 EXAMPLE AVE APT 3B : @SAMPLE DINER RYE_BROOK"]
    for k, text in enumerate(texts):
        rig.feed.put(f"A{k}", type="ALARM/COMM", location=text, units=("E60",), times={"dispatched": T0 + 3600 * k},
                     extra={"town": "FARAWAYTON CT"} if "FARAWAYTON" in text else {})
        rig.at(3600 * k)
        rig.poll()
    got = keyed(rig)
    rows = rig.cad.store.q("SELECT * FROM incidents ORDER BY id")
    assert len(rows) == len(texts)
    for r, text in zip(rows, texts):
        assert r["address_raw"] == text
        was = {**r, "address": shown_location(text), "address_raw": ""}          # the row as main stored it
        want = addrkey.key_incident(was, L.geo_town_aliases, L.town_of, rig.cad.geocoder, L.event_town(was)).as_dict()
        assert got[r["id"]] == tuple(want[c] for c in ("addr_key", "bldg_key", "addr_street", "addr_how", "addr_label")), text
    # a page's: its heard words, as before
    page = rig.cad.store.create_incident(address="12 Sample Ln Apt 3, Rye Brook", address_raw="12 Sample Lane apartment 3",
                                         source="60 Control page", status="queued")
    was = {"address": "12 Sample Lane apartment 3"}
    want = addrkey.key_incident(was, L.geo_town_aliases, L.town_of, rig.cad.geocoder, L.event_town(was)).as_dict()
    assert keyed(rig)[page][:2] == (want["addr_key"], want["bldg_key"])


def test_the_backfill_does_nothing_while_a_key_would_change(tmp_path, monkeypatch):
    """The read-only check: were the keys ever made from another form, the notes stored under them would be counted,
    said in the log, and no row normalised."""
    logs: list[str] = []
    rig = Rig(tmp_path)
    rig.cad.sources.log = logs.append
    a = old_row(rig, "18 Other Rd", ref="E1", feed_text="18 OTHER RD")
    b = old_row(rig, "12 Example Ave", ref="E2", feed_text="12 EXAMPLE AVE")
    before = keyed(rig)
    now = rig.clock()
    with rig.cad.store.tx() as db:
        for _ in range(2):
            db.execute("INSERT INTO premise_notes (bldg_key, addr_key, label, category, text, created_at, updated_at) "
                       "VALUES (?, ?, '', 'hazard', 'made-up note', ?, ?)", (before[a][1], before[a][0], now, now))
        db.execute("INSERT INTO preplan_points (bldg_key, kind, label, lat, lon, cell, created_at, updated_at) "
                   "VALUES (?, 'fdc', 'made-up', 41.0, -73.6, '', ?, ?)", (before[a][1], now, now))
    monkeypatch.setattr(src, "key_address", lambda inc, fed: inc["address_raw"] + ", Port Chester")   # the keys of the
    assert rig.cad.sources.backfill_addresses() == 0                             # normalised form: a town gained
    assert logs == ["incident sources: address backfill: 3 premise notes / pre-plan rows whose address key would change "
                    "(1 keyed rows checked); nothing normalised"]
    assert [rig.cad.store.incident(i)["address_raw"] for i in (a, b)] == ["", ""] and keyed(rig) == before
