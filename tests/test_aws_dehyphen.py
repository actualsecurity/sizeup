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
    "Exampletown full depart, 40 Example Road", "Exampletown Fire Department, full the part. 40 Example Road",
    "Exampletown full dept 40 Example Road", "Exampletown-Fire full apartment 40 Example Road",
    "40 Example Road, full dept", "Sampleville, general alarm"])
def test_full_department_is_heard_through_its_manglings(text):
    assert scope.said_full(text, ["Exampletown FD"])


@pytest.mark.parametrize("text", [
    "Exampletown full of smoke", "Exampletown, full report to follow", "Exampletown full party", "Exampletown full 40",
    "Sampleville full of pots",                                        # not after one of the page's departments
    "the basement is full of pots", "Exampletown is full of pots", "full of pots", ""])
def test_full_department_is_not_heard_where_it_was_not_said(text):
    assert not scope.said_full(text, ["Exampletown FD"])


def test_a_mangled_full_department_upgrades_a_call_from_specific_units(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    iid = rig.page(text="Port Chester Fire Department, Engine 60, Ladder 31, respond to an activated fire alarm. "
                        "12 Example Avenue, cross of Sample Street. Time out 1432.")["id"]
    assert rig.cad.full(iid)["scope"]["short"] == "Specific units"
    rig.at(60)
    rig.page(pid="p2", text="Port-Chester full of pots, activated fire alarm. 12 12 Example-Avenue, cross of Sample-Street.")
    assert rig.cad.full(iid)["scope"]["short"] == "Full dept"


# -- the tones' default ---------------------------------------------------------------------------------------------------


def test_specific_units_only_when_the_page_names_them():
    tones = ["Exampletown FD"]
    assert scope.from_page(tones, ["Engine 60"], "Fire alarm") == (scope.UNITS, [])
    assert scope.from_page(tones, [], "Fire alarm") == (scope.FULL, ["Exampletown FD"])
    assert scope.from_page(tones, [], "Unknown", AWS_PAGE) == (scope.FULL, ["Exampletown FD"])   # no type heard
    assert scope.from_page(tones, [], "Unknown") == (scope.FULL, ["Exampletown FD"])             # the all-call alone
    assert scope.from_page(["Exampletown · Fire (est.)"], [], "Unknown") == (scope.FULL, ["Exampletown · Fire"])
    assert scope.from_page([], [], "Fire alarm") is None               # no tones: nobody's


def test_a_roles_tones_a_test_or_an_announcement_are_no_full_department():
    assert scope.all_call(["Exampletown FD (heard)", "Exampletown · Fire", "Exampletown · Fire · Chiefs",
                           "Exampletown · EMS"]) == ["Exampletown FD", "Exampletown · Fire"]
    assert scope.from_page(["Exampletown · Fire · Chiefs"], [], "Unknown", "chiefs, call the office") is None
    assert scope.from_page(["Exampletown · EMS"], [], "Unknown") is None
    for text in ("Exampletown on a test, test only", "Exampletown, drill tonight at 1900", "this is a test page"):
        assert scope.from_page(["Exampletown FD"], [], "Unknown", text) is None
    # ... but a role's tones with a call type, or the words, still are (as before)
    assert scope.from_page(["Exampletown · Fire · Chiefs"], [], "Fire alarm") == (scope.FULL, ["Exampletown · Fire"])
    assert scope.from_page(["Exampletown · Fire · Chiefs"], [], "Unknown", "Exampletown full of pots") == \
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
    assert (s["short"], s["source"], s["departments"]) == ("Full dept", "page", ["Port Chester FD"])


# -- the feed's department-level dispatch over the page's reading ---------------------------------------------------------------


def test_what_a_feeds_report_says_about_who_was_dispatched():
    entries = {"DPTA": "Exampletown FD"}
    ph = lambda u: "Sampleville FD" if str(u).startswith("PH") else ""    # noqa: E731
    assert scope.from_units(["DPTA", "E1"], entries) == (scope.FULL, ["Exampletown FD"])
    assert scope.from_units(["PH1", "E1"], entries, ph) == (scope.FULL, ["Sampleville FD"])     # a placeholder
    assert scope.from_units(["E1"], entries, ph, [(T0, "~GENERAL FIRE ALARM")]) == (scope.FULL, [])
    assert scope.from_units([], entries, ph, [(T0, "caller reports an alarm sounding")]) is None
    assert scope.from_units(["E1"], entries, ph, [(T0, "E1 responding")]) == (scope.UNITS, [])


def test_which_reading_is_shown():
    page_units, page_full = (scope.UNITS, []), (scope.FULL, ["Exampletown FD"])
    feed_full, feed_other = (scope.FULL, ["Exampletown FD"]), (scope.FULL, ["Sampleville FD"])
    tones = ["Exampletown FD"]
    assert scope.shown(page_units, feed_full, tones) == ("feed", feed_full)          # the primary's dispatch wins
    assert scope.shown(None, feed_full) == ("feed", feed_full)
    assert scope.shown(page_units, feed_other, tones) == ("page", page_units)        # mutual aid: another department's
    assert scope.shown(page_units, (scope.FULL, []), tones) == ("page", page_units)  # a comment alone: not over apparatus
    assert scope.shown(page_full, feed_other, tones) == ("page", (scope.FULL, ["Exampletown FD", "Sampleville FD"]))
    assert scope.shown(page_full, (scope.UNITS, []), tones) == ("page", page_full)   # units signing on: no evidence
    assert scope.shown(page_units, (scope.UNITS, []), tones) == ("page", page_units)
    assert scope.shown(None, None) == ("", None)


def test_a_feeds_placeholder_is_a_full_department_and_stays_one_when_units_sign_on(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    assert rig.cad.lists.placeholder("DPT239")                         # the data file's department placeholder
    inc = rig.page(text="Port Chester Fire Department, Engine 60, respond to an activated fire alarm. 12 Example Avenue, "
                        "cross of Sample Street. Time out 1432.")
    assert rig.cad.full(inc["id"])["scope"]["short"] == "Specific units"   # the page's guess: apparatus heard
    rig.at(30)
    rig.feed.put("A1", type="ALARM/COMM", location=ADDR, units=("DPT239",), times={"dispatched": T0})
    rig.poll()
    assert rig.iid() == inc["id"]
    s = rig.cad.full(inc["id"])["scope"]
    assert (s["short"], s["source"], s["departments"], s["page"]) == ("Full dept", "feed", ["Port Chester FD"], "units")
    rig.at(90)
    rig.change(units=("E60", "L31"))                                   # units signed on, the placeholder gone
    assert rig.cad.full(inc["id"])["scope"]["short"] == "Full dept"
    rig.at(120)
    rig.page(pid="p2", text="Port Chester Fire Department, Engine 60, respond to an activated fire alarm. 12 Example "
                            "Avenue, cross of Sample Street. Time out 1432.")
    assert rig.cad.full(inc["id"])["scope"]["short"] == "Full dept"    # the page's repeat never takes it back


def test_a_general_alarm_comment_gives_the_scope_and_the_type_when_nothing_else_does(tmp_path):
    rig = AutoRig(tmp_path)
    rig.mode("pcfd")
    rig.feed.put("A1", type="", location=ADDR, units=("E60",), times={"dispatched": T0})
    rig.poll()
    iid = rig.iid()
    assert rig.inc()["type"] == "" and rig.inc()["scope"]["short"] == "Specific units"
    rig.at(15)
    rig.change(comments=((T0 + 10, "~GENERAL FIRE ALARM"),))
    inc = rig.inc()
    assert inc["type"] == "ALARM" and (inc["scope"]["short"], inc["scope"]["source"]) == ("Full dept", "feed")
    rig.at(30)
    rig.change(comments=((T0 + 10, "~GENERAL FIRE ALARM"),
                         (T0 + 25, "caller states there is no structure fire, an alarm is sounding in the lobby")))
    assert rig.inc()["type"] == "ALARM"                                # a narrative is never read for a type
    assert iid == rig.iid()


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
    rig.change(type="ALARM/COMM")                                      # the primary says a type: it wins
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
