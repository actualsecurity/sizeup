"""AWS writes a custom-vocabulary phrase back with its hyphens ("Example-Post-Road"): the hub writes it as the list
does when the result comes in and when a stored transcript is read (sizeup/transcribe.py Display), and who was paged
(sizeup/cad/scope.py) survives what speech-to-text does to "full department". Synthetic only: made-up towns, streets
and places in the shape of an AWS page transcript, the fake Transcribe (tests/fake_transcribe.py), a scripted feed."""

from __future__ import annotations

import pytest

from sizeup.cad import scope
from sizeup.dispatch import parse
from sizeup.incidents import IncidentStore
from sizeup.transcribe import AwsTranscriber, Display, Settings, Usage, dehyphen, display_map, respell
from tests.fake_transcribe import FakeTranscribe, tone8k
from tests.test_dispatch_modes import ADDR, AutoRig
from tests.test_incident_sources import T0
from tests.test_radios import tx
from tests.test_transcribe import Results, fake_creds, wait  # noqa: F401 - fake_creds: test credentials, local hosts only

VOCAB = ["Exampletown", "Example Post Road", "Sample Street", "Test Street", "Sample Gym", "Engine 60", "Fire 2",
         "Sample-on-Hudson", "St. Mary's Church", "N Main St", "Rye Brook"]
MAP = display_map(VOCAB)
# an AWS page transcript's shape: the town, the address, the place, the cross streets, then the read-back
AWS_PAGE = ("Exampletown full department 40 Example-Post-Road, the Sample-Gym. Sample-Street and Test-Street are "
            "commercial. Exampletown full of pots 40 40 Example Toast Rod the Sample-Gym.")


# -- de-hyphenation -----------------------------------------------------------------------------------------------------


def test_every_phrase_of_several_words_maps_back_not_only_the_ones_with_digits():
    assert MAP["example-post-road"] == "Example Post Road" and MAP["rye-brook"] == "Rye Brook"
    assert MAP["n-main-st"] == "N Main St" and MAP["st-mary's-church"] == "St. Mary's Church"
    assert "exampletown" not in MAP                                    # one word: AWS writes no hyphen
    assert MAP["engine-sixty"] == "Engine sixty" and MAP["fire-two"] == "Fire two"   # digits are respell()'s, where safe


def test_vocabulary_phrases_lose_awss_hyphens():
    assert dehyphen(AWS_PAGE, MAP) == ("Exampletown full department 40 Example Post Road, the Sample Gym. Sample Street "
                                       "and Test Street are commercial. Exampletown full of pots 40 40 Example Toast Rod "
                                       "the Sample Gym.")


def test_any_case_possessives_and_plurals():
    assert dehyphen("at SAMPLE-STREET and example-post-road", MAP) == "at Sample Street and Example Post Road"
    assert dehyphen("Sample-Street's hydrant, the Test-Streets, St-Mary's-Church's lot.", MAP) == \
        "Sample Street's hydrant, the Test Streets, St. Mary's Church's lot."
    assert dehyphen("Fire-two story frame", MAP) == "Fire two story frame"


@pytest.mark.parametrize("text", [
    "a twenty-three year-old male, T-bone accident", "10-75 the box", "Sample-on-Hudson", "mother-in-law",
    "Other-Street and Unknown-Post-Road",                              # not phrases of the vocabulary
    "Sample-Street-Extension", "pre-Sample-Street", "Sample-Street-2", "E-60", "no hyphens here", ""])
def test_any_other_hyphenated_word_stays(text):
    assert dehyphen(text, MAP) == text


def test_display_reads_the_lists_as_they_are_now():
    lists, now = [["Sample Street"]], [0.0]
    d = Display(lambda: lists[0], ttl_s=60, clock=lambda: now[0], log=lambda m: None)
    assert d("Sample-Street and Test-Street") == "Sample Street and Test-Street"
    lists[0] = ["Sample Street", "Test Street"]
    assert d("Test-Street") == "Test-Street"                           # read again only after ttl_s
    now[0] = 61.0
    assert d("Sample-Street and Test-Street") == "Sample Street and Test Street"
    d.lists = lambda: 1 / 0                                            # the lists can't be read: the last map
    now[0] = 200.0
    assert d("Test-Street") == "Test Street" and d(None) is None


def test_an_aws_result_is_written_without_the_hyphens_after_the_respelling():
    fake = FakeTranscribe(phrases={400: "Engine-sixty on location Example-Post-Road at Sample-Street"})
    res, kv = Results(), {}
    t = AwsTranscriber(Settings(endpoint=fake.url, pace=50, quiet_s=0.4), Usage(kv.get, kv.__setitem__), res.ok, res.fail,
                       log=lambda m: None, respell=lambda s: respell(s, {"engine sixty": "Engine 60"}),
                       display=Display(lambda: VOCAB, log=lambda m: None))
    try:
        t.submit("South 2", "a", tone8k(400, 0.5))
        assert wait(lambda: "a" in res.done)
        assert res.done["a"][0] == "Engine 60 on location Example Post Road at Sample Street."
    finally:
        t.stop()
        fake.close()


def test_a_late_answer_kept_as_the_alternative_is_written_the_same_way():
    late = []
    t = AwsTranscriber(Settings(), Usage(lambda k: None, lambda k, v: None), print, print, log=lambda m: None,
                       on_late=lambda item, text, conf: late.append(text), display=Display(lambda: VOCAB, log=lambda m: None))

    class Job:
        channel, item, state, deadline = "South 2", "a", "timeout", 0.0
    t._done(Job(), "40 Example-Post-Road", None)
    assert late == ["40 Example Post Road"]


# -- the page parser on an AWS transcript -----------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [AWS_PAGE, dehyphen(AWS_PAGE, MAP)])
def test_the_parser_gets_the_address_and_cross_streets_from_a_hyphenated_transcript(text):
    d = parse(text)
    assert d.location == "40 example post road"
    assert d.cross_street == "sample street and test street"
    assert d.units == []                                               # "40 40": the read-back, never apparatus


# -- stored transcripts: as shown when read, never rewritten ------------------------------------------------------------------


def test_stored_transcripts_are_shown_without_the_hyphens_and_never_rewritten(tmp_path):
    rig = AutoRig(tmp_path)
    show = Display(lambda: VOCAB, log=lambda m: None)
    rig.cad.comms.display = rig.monitor.display = show
    inc = rig.page(text="Port Chester Fire Department, respond to an activated fire alarm. 40 Example-Post-Road, cross of "
                        "Sample-Street. Time out 1432.")
    rig.cad.record_traffic(tx("307-1-call_1-t0", T0 + 5, source="Fire/EMS 15", tg=307, text="Engine 63 on Sample-Street"))
    full = rig.cad.full(inc["id"])
    assert "40 Example Post Road, cross of Sample Street" in full["remarks"] and "-Street" not in full["remarks"]
    rows = rig.cad.comms.query(channel="Fire/EMS 15")
    assert [r["text"] for r in rows if r["id"] == "307-1-call_1-t0"] == ["Engine 63 on Sample Street"]
    assert rig.monitor.state()["incidents"][0]["page_text"][0].count("Example Post Road") == 1
    # the stored rows are as AWS wrote them: no rewrite, at start or later
    assert rig.cad.store.one("SELECT text FROM radio_traffic WHERE id = '307-1-call_1-t0'")["text"] == "Engine 63 on Sample-Street"
    assert "Example-Post-Road" in rig.cad.store.incident(inc["id"])["remarks"]
    assert "Example-Post-Road" in rig.monitor.incidents[0].page_text[0]


# -- "full department" as speech-to-text hears it -----------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Exampletown full department 40 Example Road", "Exampletown full of pots 40 40 Example Road",
    "Exampletown full depart, 40 Example Road", "Exampletown Fire Department, full the part 40 Example Road",
    "Exampletown full dept 40 Example Road", "40 Example Road, full dept",
    "40 Example Road, the Sample Gym. Exampletown full of pots 40 40 Example Toast Rod"])   # in the read-back
def test_full_department_is_heard_through_its_manglings(text):
    assert scope.said_full(text, ["Exampletown FD"])


@pytest.mark.parametrize("text", [
    "Exampletown full of smoke 40 Example Road", "Exampletown, full report to follow", "Exampletown full party 40",
    "Exampletown full 40 Example Road",
    "Sampleville full of pots 40 Example Road",                        # not after one of the page's departments
    "the basement is full of pots", "Exampletown is full of pots 40", "full of pots 40", "",
    # the reviewer's: an apartment, and words that only sound alike with no house number after them
    "Exampletown full apartment complex 40 Example Road", "Exampletown full apartment fire 40 Example Road",
    "Exampletown full apartment building", "Exampletown full apartment 4 Example Road",
    "Exampletown full of pride", "Exampletown full of parts", "Exampletown full day parade",
    # taken back
    "Exampletown, cancel the full department", "not a full response", "Exampletown, no full department needed",
    "disregard, Exampletown full of pots 40 Example Road", "cancel the general alarm"])
def test_full_department_is_not_heard_where_it_was_not_said(text):
    assert not scope.said_full(text, ["Exampletown FD"])


def test_a_mangled_full_department_upgrades_a_call_from_specific_units(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    iid = rig.page(text="Port Chester Fire Department, Engine 60, Ladder 31, respond to an activated fire alarm. "
                        "12 Example Avenue, cross of Sample Street. Time out 1432.")["id"]
    assert rig.cad.full(iid)["scope"]["short"] == "Specific units"
    rig.at(60)
    rig.page(pid="p2", text="Port-Chester full of pots 12 12 Example-Avenue, activated fire alarm, cross of Sample-Street.")
    assert rig.cad.full(iid)["scope"]["short"] == "Full dept"


# -- the tones' default ---------------------------------------------------------------------------------------------------

PLACE = "40 Example Road, the Sample Gym."


def test_specific_units_only_when_the_page_names_them():
    tones = ["Exampletown FD"]
    assert scope.from_page(tones, ["Engine 60"], "Fire alarm") == (scope.UNITS, [])
    assert scope.from_page(tones, [], "Fire alarm") == (scope.FULL, ["Exampletown FD"])
    assert scope.from_page(tones, [], "Unknown", AWS_PAGE) == (scope.FULL, ["Exampletown FD"])   # no type heard
    assert scope.from_page(tones, [], "Unknown", PLACE) == (scope.FULL, ["Exampletown FD"])      # the all-call, a place
    assert scope.from_page(["Exampletown · Fire (est.)"], [], "Unknown", PLACE) == (scope.FULL, ["Exampletown · Fire"])
    assert scope.from_page([], [], "Fire alarm", PLACE) is None        # no tones: nobody's


def test_the_tones_half_alone_or_words_with_no_place_give_no_scope():
    tones = ["Exampletown FD"]
    assert scope.from_page(tones, [], "Unknown") is None               # the tones only: the voice is still to come
    assert scope.from_page(tones, [], "Unknown", "") is None and scope.from_page(tones, [], "", "   ") is None
    assert scope.from_page(tones, [], "Unknown", "Exampletown, stand by") is None
    assert scope.has_place("40 Example-Post-Road, the Sample Gym") and scope.has_place("Sample Street at Test Street")
    assert not scope.has_place("Exampletown Fire Department, time out 1432") and not scope.has_place("")


@pytest.mark.parametrize("text", [
    "Exampletown on a test, test only, 40 Example Road", "Exampletown testing, 40 Example Road",
    "Exampletown, the weekly test from 40 Example Road", "Exampletown, drill tonight at 40 Example Road",
    "Exampletown, department meeting tonight at 40 Example Road", "attention all members, 40 Example Road at 1900",
    "Exampletown, the wake will be at 40 Example Road", "funeral detail, 40 Example Road",
    "Exampletown, disregard the previous alarm at 40 Example Road", "Exampletown, cancel the alarm at 40 Example Road",
    "Exampletown units be advised, 40 Example Road is closed", "Exampletown Engine company relocate to 40 Example Road",
    "Exampletown, an announcement: 40 Example Road"])
def test_a_page_that_is_no_dispatch_is_never_the_full_department_by_default(text):
    assert scope.from_page(["Exampletown FD"], [], "Unknown", text) is None


def test_a_roles_tones_or_another_services_are_no_all_call():
    assert scope.all_call(["Exampletown FD (heard)", "Exampletown · Fire", "Exampletown · Fire · Chiefs",
                           "Exampletown · EMS"]) == ["Exampletown FD", "Exampletown · Fire"]
    assert scope.from_page(["Exampletown · Fire · Chiefs"], [], "Unknown", PLACE) is None
    assert scope.from_page(["Exampletown · EMS"], [], "Unknown", PLACE) is None
    # ... but a role's tones with a call type, or the words, still are (as before)
    assert scope.from_page(["Exampletown · Fire · Chiefs"], [], "Fire alarm") == (scope.FULL, ["Exampletown · Fire"])
    assert scope.from_page(["Exampletown · Fire · Chiefs"], [], "Unknown", "Exampletown full of pots 40 Example Road") == \
        (scope.FULL, ["Exampletown · Fire"])


def test_a_typeless_page_on_the_departments_tones_is_a_full_department_not_the_feeds_units(tmp_path):
    """The regression: no type heard on the page, the feed listing the units that signed on. Never "Specific units"."""
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    rig.feed.put("A1", type="", location=ADDR, units=("E60",), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    assert rig.cad.full(iid)["scope"]["short"] == "Specific units"     # the feed's apparatus, no page yet
    rig.at(20)
    rig.page(text="Port-Chester full of pots 12 12 Example-Avenue, the Sample-Gym. Sample-Street and Test-Street are commercial.")
    s = rig.cad.full(iid)["scope"]
    assert (s["short"], s["source"], s["departments"], s["feed"]) == ("Full dept", "page", ["Port Chester FD"], "units")


# -- the feed's reading beside the page's ---------------------------------------------------------------------------------------


def test_what_a_feeds_report_says_about_who_was_dispatched():
    entries = {"DPTA": "Exampletown FD"}
    ph = lambda u: "Sampleville FD" if str(u).startswith("PH") else ""    # noqa: E731
    assert scope.from_units(["DPTA", "E1"], entries) == (scope.FULL, ["Exampletown FD"])     # a department entry
    assert scope.from_units(["E1"], entries, ph, [(T0, "E1 responding")]) == (scope.UNITS, [])
    assert scope.from_units([], entries, ph, [(T0, "caller reports an alarm sounding")]) is None
    assert scope.from_units(["E1"], entries, ph, [(T0, "~FULL DEPARTMENT")]) == (scope.FULL, [])
    assert scope.from_units(["E1"], entries, ph, [(T0, "E1 on location"), (T0 + 5, "~ALL COMPANIES")]) == (scope.FULL, [])


@pytest.mark.parametrize("comment", [
    "~GENERAL FIRE ALARM",                                             # a call type (an alarm), not who was sent
    "GENERAL FIRE ALARM ACTIVATION 2ND FLOOR", "~CANCEL GENERAL ALARM ASSIGNMENT", "~NOT A FULL DEPARTMENT RESPONSE",
    "~FULL RESPONSE NOT REQUIRED", "~HOLD THE FULL DEPARTMENT", "~FULL DEPARTMENT PER THE CHIEF",
    "the caller asked whether the full department is coming to the building tonight"])
def test_a_comment_that_is_no_dispatch_line_is_never_a_full_department(comment):
    assert scope.from_units(["E1"], {}, None, [(T0, comment)]) == (scope.UNITS, [])
    assert scope.from_units(["E1"], {}, None, [(T0, "E1 responding"), (T0 + 5, comment.lstrip("~"))]) == (scope.UNITS, [])


def test_a_placeholder_with_apparatus_listed_says_only_what_the_list_says():
    """Whether the county puts the placeholder on a single-unit call too isn't known: with apparatus, never "full"."""
    ph = lambda u: "Sampleville FD" if str(u).startswith("PH") else ""    # noqa: E731
    from sizeup.cad import sources
    sources.set_placeholders(lambda c: str(c).startswith("PH"))
    try:
        assert scope.from_units(["PH1", "E1"], {}, ph) == (scope.UNITS, [])
        assert scope.from_units(["PH1"], {}, ph) == (scope.FULL, ["Sampleville FD"])
    finally:
        sources.set_placeholders(None)


def test_which_reading_is_shown():
    page_units, page_full = (scope.UNITS, []), (scope.FULL, ["Exampletown FD"])
    feed_full, feed_other = (scope.FULL, ["Exampletown FD"]), (scope.FULL, ["Sampleville FD"])
    assert scope.shown(page_units, feed_full) == ("page", page_units)                # apparatus a page named stands
    assert scope.shown(page_units, feed_other) == ("page", page_units)               # ... over another department's too
    assert scope.shown(page_units, (scope.FULL, [])) == ("page", page_units)
    assert scope.shown(None, feed_full) == ("feed", feed_full)                       # no reading from the page
    assert scope.shown(page_full, feed_other) == ("page", (scope.FULL, ["Exampletown FD", "Sampleville FD"]))
    assert scope.shown(page_full, (scope.UNITS, [])) == ("page", page_full)          # units signing on: no evidence
    assert scope.shown(None, (scope.UNITS, [])) == ("feed", (scope.UNITS, []))
    assert scope.shown(None, None) == ("", None)


def test_a_feeds_placeholder_never_overrides_a_page_naming_apparatus(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    assert rig.cad.lists.placeholder("DPT239")                         # the data file's department placeholder
    inc = rig.page(text="Port Chester Fire Department, Engine 60, respond to an activated fire alarm. 12 Example Avenue, "
                        "cross of Sample Street. Time out 1432.")
    rig.at(30)
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("DPT239",), times={"dispatched": T0})
    rig.poll()
    assert rig.iid() == inc["id"]
    s = rig.cad.full(inc["id"])["scope"]
    assert (s["short"], s["source"], s["feed"]) == ("Specific units", "page", "all PCFD")   # the feed's: in the hover


def test_a_feeds_placeholder_supplies_the_full_department_when_the_page_gives_no_reading(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("DPT239",), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    s = rig.cad.full(iid)["scope"]
    assert (s["short"], s["source"], s["departments"]) == ("Full dept", "feed", ["Port Chester FD"])
    rig.at(20)
    rig.page(text="Port Chester Fire Department, stand by.")           # tones and words, nothing to read: still the feed's
    s = rig.cad.full(iid)["scope"]
    assert (s["short"], s["source"]) == ("Full dept", "feed")


# -- the type: the feed's, a type-line comment for an incident with none, the page as the fallback ------------------------------


def test_a_type_line_comment_types_an_incident_that_has_no_type(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    rig.feed.put("A1", type="", location=ADDR, units=("E60",), times={"dispatched": T0})
    rig.poll()
    assert rig.inc()["type"] == ""
    rig.at(15)
    rig.change(comments=((T0 + 10, "~GENERAL FIRE ALARM"),))
    inc = rig.inc()
    assert inc["type"] == "ALARM" and inc["scope"]["short"] == "Specific units"   # the type only: never who was sent


@pytest.mark.parametrize("comment", ["NOT A STRUCTURE FIRE", "~NOT A STRUCTURE FIRE", "HOLD FOR MVA", "~HOLD FOR MVA",
                                     "CANCEL FIRE ALARM", "~CANCEL FIRE ALARM", "CAR FIRE EXTINGUISHED", "~CAR FIRE OUT",
                                     "~FIRE ALARM RESET", "~NO STRUCTURE FIRE", "~FIRE ALARM PER KEYHOLDER",
                                     "~DISREGARD THE FIRE ALARM",
                                     "caller states there is a structure fire somewhere down the block from here"])
def test_a_negation_or_a_status_note_is_never_a_type(tmp_path, comment):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    rig.feed.put("A1", type="", location=ADDR, units=("E60",), times={"dispatched": T0}, comments=((T0 + 1, comment),))
    rig.poll()
    assert rig.inc()["type"] == ""


def test_a_later_unmarked_comment_is_never_a_type(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    rig.feed.put("A1", type="", location=ADDR, units=("E60",), times={"dispatched": T0},
                 comments=((T0 + 1, "E60 responding"),))
    rig.poll()
    rig.at(15)
    rig.change(comments=((T0 + 1, "E60 responding"), (T0 + 10, "STRUCTURE FIRE")))
    assert rig.inc()["type"] == ""


@pytest.mark.parametrize("comment", ["~STRUCTURE FIRE", "~MVA", "~CAR FIRE"])
def test_a_comment_never_retypes_an_incident_mid_call(tmp_path, comment):
    """A type already there stays: the feed's own, the page's, a person's, an earlier comment's."""
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    rig.feed.put("A1", type="", location=ADDR, units=("E60",), times={"dispatched": T0},
                 comments=((T0 + 1, "~GENERAL FIRE ALARM"),))
    rig.poll()
    assert rig.inc()["type"] == "ALARM"                                # an earlier comment's
    rig.at(15)
    rig.change(comments=((T0 + 1, "~GENERAL FIRE ALARM"), (T0 + 10, comment)))
    assert rig.inc()["type"] == "ALARM"

    feed = AutoRig(tmp_path / "feed")                                  # the feed's own type field
    feed.mode("pcfd")
    feed.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("E60",), times={"dispatched": T0},
                  comments=((T0 + 1, comment),))
    feed.poll()
    assert (feed.inc()["type"], feed.inc()["subtype"]) == ("ALARM", "COMM")

    page = AutoRig(tmp_path / "page")                                  # the page's (the fallback)
    page.mode("pcfd")
    page.feed.put("A1", type="", location=ADDR, units=("E60",), times={"dispatched": T0})
    page.poll()
    page.at(20)
    page.page(text="Port Chester Fire Department, respond to a reported gas leak. 12 Example Avenue, cross of Sample "
                   "Street. Time out 1432.")
    assert page.inc()["type"] == "HAZARD"
    page.at(40)
    page.change(comments=((T0 + 30, comment),))
    assert page.inc()["type"] == "HAZARD"


def test_the_pages_type_is_the_fallback_until_the_feed_says_one(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    rig.feed.put("A1", type="", location=ADDR, units=("DPT239",), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    assert rig.inc()["type"] == ""
    rig.at(20)
    got = rig.page(text="Port Chester Fire Department, respond to a reported structure fire. 12 Example Avenue, cross of "
                        "Sample Street. Time out 1432.")
    assert got["id"] == iid and rig.inc()["type"] == "STRU"            # the backup fills what the primary left empty
    rig.at(40)
    rig.change(type="ALARM/COMM")                                      # the primary's own type field: it wins
    inc = rig.inc()
    assert (inc["type"], inc["subtype"]) == ("ALARM", "COMM")


def test_a_persons_type_stands_over_the_pages_fallback_and_a_comment(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    rig.feed.put("A1", type="", location=ADDR, units=("DPT239",), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    rig.cad.update(iid, {"type": "MVA", "operator": "Lt. Test"})
    rig.at(20)
    rig.page(text="Port Chester Fire Department, respond to a reported structure fire. 12 Example Avenue, cross of "
                  "Sample Street. Time out 1432.")
    rig.at(30)
    rig.change(comments=((T0 + 25, "~GENERAL FIRE ALARM"),))
    assert rig.inc()["type"] == "MVA"


def test_the_monitor_shows_its_pages_as_stored_without_a_display():
    m = IncidentStore({"Exampletown FD"}, geocode=lambda d: None)
    m.on_page("p1", ["Exampletown FD"], parse(AWS_PAGE), at=T0)
    assert m.state()["incidents"][0]["page_text"] == [AWS_PAGE]


# -- the Radio log's search: a hyphen and a space the same -----------------------------------------------------------------------


def test_the_radio_search_finds_rows_stored_with_and_without_the_hyphens(tmp_path):
    rig = AutoRig(tmp_path)
    rig.cad.record_traffic(tx("307-1-call_1-t0", T0 + 5, source="Fire/EMS 15", tg=307, text="Engine 63 on Sample-Street"))
    rig.cad.record_traffic(tx("307-2-call_2-t0", T0 + 9, source="Fire/EMS 15", tg=307, text="Engine 63 on Sample Street"))
    rig.cad.record_traffic(tx("307-3-call_3-t0", T0 + 12, source="Fire/EMS 15", tg=307, text="Engine 63 on Other Street"))
    both = {"307-1-call_1-t0", "307-2-call_2-t0"}
    for q in ("sample street", "Sample-Street", "sample-street on"):
        assert {r["id"] for r in rig.cad.comms.query(text=q, kind="radio")} == both, q
    assert rig.cad.comms.query(text="sample-avenue", kind="radio") == []


# -- a display that fails never costs the respelling --------------------------------------------------------------------------


def test_a_failing_display_keeps_the_respelled_text():
    done, logs = [], []
    t = AwsTranscriber(Settings(), Usage(lambda k: None, lambda k, v: None), lambda item, text, conf: done.append(text),
                       print, log=logs.append, respell=lambda s: respell(s, {"engine sixty": "Engine 60"}),
                       display=lambda s: 1 / 0)

    class Job:
        channel, item, state, deadline = "South 2", "a", "open", 0.0
    job = Job()
    t._open.add(job)
    t._done(job, "Engine-sixty on Sample-Street", None)
    assert done == ["Engine 60 on Sample-Street"] and any("vocabulary display" in m for m in logs)


# -- cross streets said with no "between" (sizeup/geo/pagegeo.py _implied) ----------------------------------------------------


@pytest.fixture(scope="module")
def town(tmp_path_factory):
    """A made-up grid as a gazetteer of its own: Example Post Rd running east-west with 1-99 along it, Sample St and
    Test St crossing it either side of number 40, Other St and Far Ave crossing each other a kilometre north."""
    import json
    import sqlite3
    from sizeup.geo.gazetteer import SCHEMA, Gazetteer
    lat, w, e = 41.0, -73.690, -73.680
    lon = lambda n: w + (e - w) * n / 100                                  # noqa: E731
    path = tmp_path_factory.mktemp("gaz") / "town.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", [
        (1, "example post rd", "Example Post Rd"), (2, "sample st", "Sample St"), (3, "test st", "Test St"),
        (4, "other st", "Other St"), (5, "far ave", "Far Ave")])
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,1)",
                   [(1, n, "", "Port Chester", lat, round(lon(n), 6)) for n in range(1, 100)])
    lines = [(1, [[w, lat], [e, lat]]), (2, [[lon(30), lat - 0.002], [lon(30), lat + 0.002]]),
             (3, [[lon(50), lat - 0.002], [lon(50), lat + 0.002]]), (4, [[w, lat + 0.01], [e, lat + 0.01]]),
             (5, [[lon(40), lat + 0.008], [lon(40), lat + 0.012]])]
    db.executemany("INSERT INTO lines VALUES (?,?,?)", [(sid, "Port Chester", json.dumps(c)) for sid, c in lines])
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)", [
        (1, 2, lat, lon(30), "Port Chester"), (1, 3, lat, lon(50), "Port Chester"), (4, 5, lat + 0.01, lon(40), "Port Chester")])
    db.commit()
    db.close()
    return Gazetteer(path)


def test_two_streets_after_the_address_that_both_meet_it_are_its_cross_streets(town):
    from sizeup.geo.pagegeo import locate_page
    for text in ("40 Example-Post-Road, the Sample Gym. Sample-Street and Test-Street are commercial.",
                 "40 Example Post Road, the Sample Gym. Sample Street and Test Street are commercial."):
        pg = locate_page(town, text, ["Port Chester FD"])
        b = pg.best
        assert pg.confirmed and b.match.label == "40 Example Post Rd, Port Chester", (text, pg.why)
        assert b.checked == "all" and set(b.crosses) == {"Sample St", "Test St"}
        assert pg.crosses_heard == ["sample st", "test st"]


def test_a_pair_that_does_not_meet_the_address_street_changes_nothing(town):
    from sizeup.geo.pagegeo import locate_page
    plain = locate_page(town, "40 Example Post Road, the Sample Gym.", ["Port Chester FD"])
    for pair in ("Other Street and Far Avenue are commercial.",        # they meet each other, never this street
                 "Sample Street and Far Avenue are commercial.",       # one meets it: not a pair of its cross streets
                 "Example Post Road and Test Street are commercial."): # the address street itself
        pg = locate_page(town, f"40 Example Post Road, the Sample Gym. {pair}", ["Port Chester FD"])
        assert (pg.best.match.label, pg.best.match.lat, pg.best.match.lon, pg.best.checked, pg.best.score, pg.confirmed) == \
            (plain.best.match.label, plain.best.match.lat, plain.best.match.lon, "", plain.best.score, plain.confirmed), pair
    far = locate_page(town, "40 Example Post Road, the Sample Gym. Units are responding from quarters now and then "
                            "Sample Street and Test Street were mentioned.", ["Port Chester FD"])
    assert far.best.checked == ""                                      # too far after the address: not its cross streets
