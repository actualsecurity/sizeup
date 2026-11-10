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
    assert a["named"] == [{"hm": "16:38", "from": "Example Town", "to": "Grand Central"}]
    assert TT._alert({"header": "<p> </p>"}) is None                      # nothing left once the tags are gone


@pytest.mark.parametrize("text, brief", [
    ("The 9:05am train is operating 10 - 15 minutes late due to signal problems.", "10-15 min late (signal)"),
    ("Expect delays of up to 20 minutes due to police activity.", "delays up to 20 min (police)"),
    ("The 7:12pm train has been cancelled.", "cancelled"),
    ("Customers should allow extra time.", "Customers should allow extra time."),
])
def test_an_alert_in_a_few_words(text, brief):
    assert TT.brief(text) == brief


# ---- which train an alert is about -----------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def names():
    m = Model(parse_static(GTFS))
    return {s["code"]: s["name"] for s in m.stations.values() if s.get("code")}


def rec(num):
    return next(r for r in TT.parse_locations(LOC)["records"] if r["train"] == num)


# 9901: Stamford 11:08 -> Grand Central (westbound); 9902: Grand Central 10:53 -> New Haven; 9906: New Haven 11:00 ->
# Grand Central (America/New_York)
@pytest.mark.parametrize("text, num, mine", [
    ("The 11:08am train from Stamford to Grand Central is 10 minutes late.", "9901", True),     # the same trip
    ("The 11:08am train from Stamford to Grand Central is 10 minutes late.", "9902", False),
    ("The 11:08 a.m. train is 10 minutes late.", "9901", True),                                  # no station: its origin
    ("The 4:38pm train from New Haven-State St to Grand Central is 20 minutes late.", "9901", False),   # another train
    ("The 4:38pm train from New Haven-State St to Grand Central is 20 minutes late.", "9906", False),
    ("The 11:00am train from New Haven-State St to Grand Central is late.", "9906", False),     # New Haven, not State St
    ("The 11:00am train from New Haven to Grand Central is late.", "9906", True),
    ("The 11:08am train from Stamford to New Haven is late.", "9901", False),                    # not where it goes
    ("Train 9902 is cancelled.", "9902", True),                                                  # by number
    ("Train #9902 is cancelled.", "9901", False),
    ("Delays of up to 15 minutes on the New Haven Line.", "9901", False),                        # the whole line
])
def test_an_alert_is_this_trains_only_when_it_names_this_train(names, text, num, mine):
    assert TT.about_train(TT.named_trains(text), rec(num), names) is mine


def test_a_train_with_no_stops_cant_tell_so_the_alert_is_the_lines(names):
    r = dict(rec("9901"), stops=[])
    assert TT.about_train(TT.named_trains("The 11:08am train from Stamford is late."), r, names) is False


def test_a_train_number_isnt_a_minutes_count():
    assert TT.named_trains("Trains 20-25 minutes late") == []
    assert TT.named_trains("train 1234 minutes") == []


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
