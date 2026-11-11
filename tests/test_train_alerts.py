"""Metro-North's alerts in a train's box (#385): the MTA's HTML read as plain text on the hub (never shown as HTML),
an alert that names a train ("the 4:38pm train from X to Y", "Train 1234") shown as "This train" only on that
train's box and as the line's (labelled with the train it names) on the others, one terse line each with the full
text on hover.

The alerts are INVENTED, with the shape TrainTime gives (header / text as HTML, status, branches); the trains are
tests/fixtures/mta/traintime-locations.json's synthetic 99xx ones on the recorded GTFS line. No MTA."""

import json

import pytest

from sizeup import traintime as TT
from sizeup.trains import Model, parse_static
from tests.test_aircraft_follow import needs_node, run_node
from tests.test_traintime import LOC, provider, view
from tests.test_trains import GTFS


# ---- HTML -> plain text --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, text", [
    ("Plain words, no tags", "Plain words, no tags"),
    ("<p>The <strong>4:38pm train from Example Town to Grand Central</strong> is 20-25 minutes late.</p>",
     "The 4:38pm train from Example Town to Grand Central is 20-25 minutes late."),
    ("<p>First</p><p>Second</p>", "First · Second"),
    ("one<br>two<br/>three", "one · two · three"),
    ("<ul><li>a</li><li>b <em>c</em></li></ul>", "a · b c"),
    ("<div><p><span><strong><em>deep</em></strong> nest</span></p></div>", "deep nest"),
    ("Tom &amp; Jerry&nbsp;&#8211;&nbsp;&lt;b&gt;x&lt;/b&gt; &quot;q&quot; &#x2014;", "Tom & Jerry – <b>x</b> \"q\" —"),
    ("  lots \n\t of   <b> space </b>  ", "lots of space"),
    ("<p></p><p>  </p><p>only</p><p></p>", "only"),
    ("<!-- a comment -->kept", "kept"),
    ("", ""),
])
def test_alert_html_becomes_plain_text(raw, text):
    assert TT.alert_text(raw) == text


@pytest.mark.parametrize("raw, text", [
    ("<script>alert(1)</script>Delays", "Delays"),
    ("<p onclick=\"alert(1)\">Delays</p><img src=x onerror=alert(2)>", "Delays"),
    ("<a href=\"javascript:alert(3)\">Details</a>", "Details"),
    ("<style>p{background:url(javascript:x)}</style><p>ok</p>", "ok"),
    ("<iframe src=\"https://evil.example\">x</iframe>ok", "ok"),
    ("<scr<script>ipt>alert(4)</script>ok", "ipt>alert(4)ok"),   # what is left is words, not a tag
    ("<svg><script>alert(5)</script></svg>ok", "ok"),
    ("ok<script>never closed", "ok"),
    ("<p>a\u0000b​c</p>", "abc"),
])
def test_hostile_alert_html_is_inert_text(raw, text):
    out = TT.alert_text(raw)
    assert out == text
    assert "<script" not in out.lower() and "onerror" not in out and "javascript:" not in out


def test_long_alert_text_is_cut_and_the_input_bounded():
    assert TT.alert_text("<p>" + "word " * 500 + "</p>", 100).endswith("…")
    assert len(TT.alert_text("<p>" + "word " * 500 + "</p>", 100)) == 100
    assert len(TT.alert_text("x" * 100_000, 10_000)) <= TT.ALERT_RAW_MAX


def test_a_record_alert_is_plain_text_with_a_brief_and_the_train_it_names():
    a = TT._alert({"header": "<p>The <strong>4:38pm train from Example Town to Grand Central</strong> is operating 20-25 "
                             "minutes late following earlier mechanical issues at <strong>Example Town</strong>.</p>",
                   "text": "<p>The <strong>4:38pm train</strong> &amp; more.</p>", "status": "Some Delays", "status_level": 2,
                   "railroad": "MNR", "branches": ["NH"]})
    assert "<" not in a["header"] and a["header"].startswith("The 4:38pm train from Example Town to Grand Central is")
    assert a["text"] == "The 4:38pm train & more."
    assert a["brief"] == "20-25 min late (mechanical)"
    assert a["about"] == "4:38p Example Town–GCT"
    assert a["named"][0] == {"s": 0, "at": 4, "hm": "16:38", "from": "Example Town", "to": "Grand Central"}
    assert TT._alert({"header": "<p> </p>"}) is None                      # nothing left once the tags are gone


@pytest.mark.parametrize("text, brief", [
    ("The 9:05am train is operating 10 - 15 minutes late due to signal problems.", "10-15 min late (signal)"),
    ("Delays of up to 20 minutes due to police activity.", "delays up to 20 min (police)"),
    ("Expect delays of up to 20 minutes due to police activity.", "delays up to 20 min (police)"),   # assertive
    ("Delays of up to 20 minutes may occur.", "possible delays up to 20 min"),
    # recall (#389 re-review): the status's own clause; "will now operate N minutes late"; "was cancelled"; "is delayed"
    ("The 7:12pm train is 20 minutes late and will not stop at Example.", "20 min late"),
    ("The 7:12pm train will now operate 10 minutes late.", "10 min late"),
    ("The 7:12pm train was cancelled due to a disabled train.", "cancelled (disabled train)"),
    ("Train 9902 is delayed due to police activity.", "delayed (police)"),
    ("Train 9902 is not delayed.", TT.SEE),
    ("The 7:12pm train was 10 minutes late, it is now on time.", TT.SEE),
    ("The 7:12pm train has been cancelled.", "cancelled"),
    ("Train 9902 is being held at Example due to a medical emergency.", "held (medical)"),
    # negation, modality and the past assert nothing (#389 review)
    ("The 7:12pm train is no longer held.", TT.SEE),
    ("The 7:12pm train will not be cancelled.", TT.SEE),
    ("The 7:12pm train, previously cancelled, will now operate.", TT.SEE),
    ("Some trains may be cancelled.", TT.SEE),
    ("The 7:12pm train could be 10 minutes late.", TT.SEE),
    ("The 7:12pm train was held earlier.", TT.SEE),
    ("Service has resumed; trains are no longer 10 minutes late.", TT.SEE),
    # a cause by whole word only
    ("The 7:12pm train from Example State Street is 10 minutes late.", "10 min late"),
    ("The 7:12pm train is 10 minutes late; its powered cars and a screw are fine near the floodlight.", "10 min late"),
    ("The 7:12pm train is 10 minutes late due to a tree on the tracks.", "10 min late (tree)"),
    ("The 7:12pm train is 15 minutes behind the 6:50pm train.", ""),          # behind another train: not a delay
    ("Customers should allow extra time.", ""),
])
def test_an_alert_in_a_few_words(text, brief):
    assert TT.brief(text) == brief


def test_a_line_alert_with_no_status_shows_its_first_words():
    assert TT._alert({"header": "Customers should allow extra time.", "railroad": "MNR"})["brief"] == "Customers should allow extra time."


# ---- which train an alert is about -----------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def names():
    m = Model(parse_static(GTFS))
    return {s["code"]: s["name"] for s in m.stations.values() if s.get("code")}


def rec(num):
    return next(r for r in TT.parse_locations(LOC)["records"] if r["train"] == num)


def alert(text, **kw):
    return TT._alert({"header": text, "railroad": "MNR", "branches": ["NH"], "status": "Some Delays", **kw})


# 9901: Stamford 11:08 -> Grand Central (westbound, NH); 9902: Grand Central 10:53 -> New Haven (eastbound, NH);
# 9903: Grand Central -> New Canaan (NC); 9906: New Haven 11:00 -> Grand Central. Run date Wednesday 2026-10-14.
@pytest.mark.parametrize("text, num, mine", [
    ("The 11:08am train from Stamford to Grand Central is 10 minutes late.", "9901", True),     # the same trip
    ("The 11:08am train from Stamford to Grand Central is 10 minutes late.", "9902", False),
    ("The 11:08 a.m. train is 10 minutes late.", "9901", True),                                  # no station: its origin
    ("The 4:38pm train from New Haven-State St to Grand Central is 20 minutes late.", "9901", False),   # another train
    ("The 4:38pm train from New Haven-State St to Grand Central is 20 minutes late.", "9906", False),
    ("The 11:00am train from New Haven-State St to Grand Central is late.", "9906", False),     # New Haven, not State St
    ("The 11:00am train from New Haven to Grand Central is late.", "9906", True),
    ("The 11:08am train from Stamford to New Haven is late.", "9901", False),                    # not where it goes
    ("The 11:08am train from Stamford is late.", "9901", True),                                  # from its origin
    ("The 11:18am train from Greenwich is late.", "9901", False),                                # a station alone: origin only
    ("The 10:53am train from Grand Central is late.", "9902", True),
    ("The 11:08am train to Grand Central is late.", "9901", True),
    ("The 11:08am train to Stamford is late.", "9901", False),                                   # the other way
    ("Train 9902 is cancelled.", "9902", True),                                                  # by number
    ("Train #9902 is cancelled.", "9901", False),
    ("Delays of up to 15 minutes on the New Haven Line.", "9901", False),                        # the whole line
    ("The 6:50pm train is late. The 11:08am train is 15 minutes behind the 6:50pm train.", "9901", True),
    ("The 11:08am train is 15 minutes behind the 6:50pm train.", "9901", True),
    ("Tomorrow, the 11:08am train from Stamford will be cancelled.", "9901", False),             # another day
    ("On Saturday, the 11:08am train from Stamford will be cancelled.", "9901", False),
    ("On Wednesday, the 11:08am train from Stamford will be cancelled.", "9901", True),
])
def test_an_alert_is_this_trains_only_when_it_names_this_train(names, text, num, mine):
    assert bool(TT.about_train(alert(text), rec(num), names)) is mine


def test_no_station_named_needs_the_trains_branch(names):
    r = rec("9903")                                                    # New Canaan branch, from Grand Central 10:53
    assert TT.about_train(alert("The 10:53am train is late.", branches=["NH"]), r, names) == ()
    assert TT.about_train(alert("The 10:53am train is late.", branches=["NC"]), r, names) == (0,)


def test_an_alert_with_no_railroad_or_planned_for_another_day_isnt_this_trains(names):
    assert TT.about_train(alert("Train 9901 is cancelled.", railroad="", branches=[]), rec("9901"), names) == ()
    assert TT.about_train(alert("Train 9901 is cancelled.", railroad="", branches=["NH"]), rec("9901"), names) == (0,)
    assert TT.about_train(alert("Train 9901 is cancelled.", railroad="LIRR", branches=["BY"]), rec("9901"), names) == ()
    assert TT.about_train(alert("Train 9901 is cancelled.", is_planned=True, start_time=1791990507 + 86400), rec("9901"), names) == ()
    assert TT.about_train(alert("Train 9901 is cancelled.", is_planned=True, start_time=1791990507), rec("9901"), names) == (0,)
    assert TT.branch_alerts([{"alerts": [alert("x", railroad="", branches=[])]}]) == []


def test_the_first_stop_is_the_trips_own(names):
    r = rec("9901")
    r = dict(r, stops=[dict(r["stops"][0], sched=None)] + r["stops"][1:])    # no time at its first stop: can't tell
    assert TT.about_train(alert("The 11:18am train from Greenwich is late."), r, names) == ()
    assert TT.about_train(alert("The 11:08am train is late."), r, names) == ()


def test_a_train_with_no_stops_cant_tell_so_the_alert_is_the_lines(names):
    r = dict(rec("9901"), stops=[])
    assert TT.about_train(alert("The 11:08am train from Stamford is late."), r, names) == ()


def test_a_train_number_isnt_a_minutes_count_or_a_year():
    assert TT.named_trains("Trains 20-25 minutes late") == ()
    assert TT.named_trains("train 1234 minutes") == ()
    assert TT.named_trains("The train 2026 schedule changes.") == ()
    assert TT.named_trains("Trains 9901 and 9902 are late.") == ()                  # "trains": none in particular
    assert [n["num"] for n in TT.named_trains("Train #2026 is late. Train no. 9901 too.")] == ["2026", "9901"]
    assert TT.named_trains("Train 12345 is late.") == ()                              # not a Metro-North number


@pytest.mark.parametrize("text, brief", [
    # only the sentence that names this train: the other train's delay isn't its
    ("The 6:50pm train is 30 minutes late due to police activity. The 11:08am train from Stamford is 10 minutes late.",
     "10 min late"),
    ("The 6:50pm train is 30 minutes late. The 11:08am train from Stamford will make all local stops.", TT.SEE),
    ("The 11:08am train is 15 minutes behind the 6:50pm train due to signal problems.", TT.SEE),
    ("The 6:50pm train and the 11:08am train are 20 minutes late.", TT.SEE),        # two trains in one sentence
    ("The 11:08am train from Stamford will not be cancelled.", TT.SEE),
    ("The 11:08am train from Stamford is no longer held.", TT.SEE),
])
def test_this_trains_brief_is_from_its_own_sentence(names, text, brief):
    a = alert(text)
    m = TT.mine(a, rec("9901"), names)
    assert m is not None and m["brief"] == brief


def test_alert_words_are_parsed_once_per_poll():
    TT._alert_words.cache_clear()
    for _ in range(3):
        alert("<p>The <b>11:08am train</b> is 10 minutes late.</p>")
    info = TT._alert_words.cache_info()
    assert info.misses == 1 and info.hits == 2


# ---- the hub's answer and the box ------------------------------------------------------------------------------------

A_THIS = {"header": "<p>The <strong>11:08am train from Stamford to Grand Central</strong> is operating 10-15 minutes late "
                    "following earlier signal problems at <strong>Stamford</strong>.</p>",
          "text": "", "status": "Some Delays", "status_level": 2, "railroad": "MNR", "branches": ["NH"], "is_planned": False}
A_OTHER = {"header": "<p>The <strong>4:38pm train from New Haven-State St to Grand Central</strong> is operating 20-25 "
                     "minutes late following earlier mechanical issues at <strong>New Haven</strong>.</p>",
           "text": "", "status": "Some Delays", "status_level": 3, "railroad": "MNR", "branches": ["NH"], "is_planned": False}
A_LINE = {"header": "Delays of up to 15 minutes &amp; more due to switch trouble&nbsp;near <em>Example</em>.",
          "text": "<p>Allow extra time.</p><script>alert(1)</script>", "status": "Some Delays", "status_level": 2,
          "railroad": "MNR", "branches": ["NH"], "is_planned": False}


def answer(tmp_path):
    tr, clock, fetch = provider(tmp_path)
    doc = json.loads(LOC)
    for r in doc:
        if r.get("train_num") in ("9901", "9902"):
            r["alerts"] = [A_THIS, A_OTHER, A_LINE]
    fetch.loc = json.dumps(doc).encode()
    tr.tick()
    view(tr, clock)
    tr.refresh_rt()
    tr.tt.poll()
    return tr.public()


def test_the_hub_sends_plain_text_and_each_trains_own_alerts(tmp_path):
    d = answer(tmp_path)
    blob = json.dumps(d["alerts"])
    assert "<" not in blob and "&amp;" not in blob and "script" not in blob.lower()
    assert [a["about"] for a in d["alerts"]] == ["4:38p New Haven-State St\u2013GCT", "11:08a Stamford\u2013GCT", ""]
    assert d["branch_alerts"]["NH"] == d["alerts"]                                          # each branch's, for its trains
    t = {x["train"]: x for x in d["trains"]}
    assert [a["brief"] for a in t["9901"]["tt"]["alerts"]] == ["10-15 min late (signal)"]   # its own, by time + origin
    assert t["9902"]["tt"]["alerts"] == []                                                   # none names 9902


BOX = r"""
const flush = () => new Promise((r) => setTimeout(r, 0));
const rows = () => info.find("ml-lines")[0].children.map((c) => ({ text: c.textContent, title: c.title, cls: c.className,
  more: (c.children || []).filter((x) => x && x.className === "tr-more").map((x) => [x.textContent, x.title]) }));
(async () => {
  const m = group.attach(control);
  await TrainLayer.load(); await flush();
  m.draw(true, T);
  for (const id of IN.ids) { m.showTrain(id); out[id] = rows(); }
  console.log(JSON.stringify(out)); process.exit(0);
})();
"""


@needs_node
def test_the_box_says_this_train_and_the_line_in_one_line_each_with_the_rest_on_hover(tmp_path):
    from tests.test_train_follow import LOAD_TRAINS
    from tests.test_train_motion import STATIC
    d = answer(tmp_path)
    ids = {t["train"]: t["id"] for t in d["trains"] if t["train"] in ("9901", "9902")}
    o = run_node(tmp_path, [], f"const STATIC = {json.dumps(str(STATIC) + '/')};\n" + LOAD_TRAINS + BOX,
                 {"answers": [d], "got": d["now"] * 1000, "id": ids["9901"], "ids": [ids["9901"], ids["9902"]]})
    al = lambda rs: [r for r in rs if r["cls"] == "tr-alert"]
    a1, a2 = al(o[ids["9901"]]), al(o[ids["9902"]])
    # 9901: the alert that names it as "This train"; the other two as the line's, the one about another train labelled
    assert a1[0]["text"] == "This train: 10-15 min late (signal)"
    assert "Stamford" in a1[0]["title"] and "<" not in a1[0]["title"]
    assert a1[1]["text"] == "Line: delays · 4:38p New Haven-State St–GCT 20-25 min late · +1"
    assert a1[1]["more"][0][0] == "+1" and "switch trouble near Example" in a1[1]["more"][0][1]
    assert "Allow extra time." in a1[1]["more"][0][1] and "alert(1)" not in a1[1]["more"][0][1]
    # 9902: nothing names it: no "This train"; all three the line's, none said as if about 9902
    assert len(a2) == 1 and a2[0]["text"].startswith("Line: delays · 4:38p ") and a2[0]["text"].endswith(" · +2")
    assert not any(r["text"].startswith("This train") for r in o[ids["9902"]])
    assert all("<" not in r["text"] and "<" not in r["title"] for r in a1 + a2)


def test_the_line_rows_label_and_brief_are_the_same_trains():
    a = alert("Train 9912 is cancelled. The 5:00pm train from Example Town is 15 minutes late.")
    assert (a["about"], a["brief"]) == ("Train 9912", "cancelled")
    a = alert("The 5:00pm train from Example Town is 15 minutes late. Train 9912 is cancelled.")
    assert (a["about"], a["brief"]) == ("5:00p Example Town", "15 min late")
    a = alert("The 5:00pm train from Example Town and Train 9912 are 15 minutes late.")      # two in one sentence
    assert (a["about"], a["brief"]) == ("5:00p Example Town", TT.SEE)


@pytest.mark.parametrize("text, label", [
    ("The 4:38pm train from Example Town may be cancelled.", "4:38p Example Town"),
    ("The 4:38pm train from Example Town should arrive 10 minutes late.", "4:38p Example Town"),
    ("The 4:38pm train from Example Town expected to operate late.", "4:38p Example Town"),
    ("We anticipate the 4:38pm train from Example Town to Grand Central to operate 15 minutes late.", "4:38p Example Town\u2013GCT"),
    ("The 4:38pm train from Example Town to operate express.", "4:38p Example Town"),
    ("The 4:38pm train to depart Example Town is late.", "4:38p"),
    ("The 4:38pm train to Grand Central will be held.", "4:38p GCT"),
])
def test_a_label_is_just_the_time_and_stations(text, label):
    assert alert(text)["about"] == label


def test_an_anticipated_delay_matches_and_briefs(names):
    a = alert("We anticipate the 11:08am train from Stamford to Grand Central to operate 15 minutes late.")
    assert TT.mine(a, rec("9901"), names)["brief"] == "15 min late"


@pytest.mark.parametrize("text, nums", [
    ("Train 2024 is cancelled.", ["2024"]),
    ("Train 1999 from Example Town is 10 minutes late.", ["1999"]),
    ("There is no train 2024 service.", []),
    ("The train 2026 schedule changes.", []),
    ("Starting in train 2026 is new.", []),
    ("Since March 3, train 2025 is cancelled.", []),
    ("On October train 2026 will run.", []),
])
def test_a_year_like_train_number_counts_when_a_train_is_meant(text, nums):
    assert [n["num"] for n in TT.named_trains(text)] == nums


def test_mnr_branches_and_line_wide_alerts_are_kept():
    recs = [{"railroad": "MNR", "branch_id": "NH", "alerts": [
        alert("A: listed NH, railroad unsaid", railroad="", branches=["NH"]),
        alert("B: whole railroad", branches=[]),
        alert("C: another railroad's", railroad="LIRR", branches=["BY"]),
        alert("D: railroad unsaid, no branch", railroad="", branches=[])]}]
    assert [a["header"] for a in TT.branch_alerts(recs, "NH")] == ["A: listed NH, railroad unsaid", "B: whole railroad"]
    assert [a["header"] for a in TT.branch_alerts(recs, "NC")] == ["B: whole railroad"]


@pytest.mark.parametrize("text, brief", [
    # a delay the same paragraph undoes, or said in the past: no brief (#389 round 3)
    ("The 11:08am train from Stamford was 10 minutes late; it is now on time.", TT.SEE),
    ("The 11:08am train from Stamford was 10 minutes late, it is now on time.", TT.SEE),
    ("The 11:08am train from Stamford is 10 minutes late. It is now on time.", TT.SEE),
    ("The 11:08am train from Stamford is 10 minutes late. Service has since been restored.", TT.SEE),
    ("The 11:08am train from Stamford was 10 minutes late.", TT.SEE),
    ("The 11:08am train from Stamford is 10 minutes late. It will make all stops.", "10 min late"),
    ("The 11:08am train from Stamford will now operate 10 minutes late.", "10 min late"),
    # "will not operate": cancelled
    ("The 11:08am train from Stamford will not operate.", "cancelled"),
    ("The 11:08am train from Stamford won't run today due to a crew shortage.", "cancelled (crew)"),
    ("The 11:08am train from Stamford may not operate.", TT.SEE),
])
def test_this_trains_brief_reads_its_paragraph(names, text, brief):
    assert TT.mine(alert(text), rec("9901"), names)["brief"] == brief


def test_a_delay_undone_in_another_paragraph_still_stands(names):
    a = alert("<p>The 11:08am train from Stamford is 10 minutes late.</p><p>Other trains are now on time.</p>")
    assert TT.mine(a, rec("9901"), names)["brief"] == "10 min late"


@pytest.mark.parametrize("text", ["Some trains are cancelled due to a disabled train.", "Trains 9911 and 9912 are held.",
                                  "Cancelled: express service."])
def test_a_line_alert_that_only_says_cancelled_or_held_says_see_alert(text):
    a = alert(text)
    assert a["about"] == "" and a["brief"] == TT.SEE


def test_another_railroads_alert_isnt_metro_norths_whatever_its_branch_code(names):
    assert not TT.is_mnr(alert("x", railroad="LIRR", branches=["NH"]), {"NH"})
    assert TT.is_mnr(alert("x", railroad="", branches=["NH"]), {"NH"})
    assert TT.about_train(alert("Train 9901 is cancelled.", railroad="LIRR", branches=["NH"]), rec("9901"), names) == ()
    assert TT.branch_alerts([{"railroad": "MNR", "branch_id": "NH", "alerts": [alert("x", railroad="LIRR", branches=["NH"])]}]) == []
