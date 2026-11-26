"""Another call inside an open one's window (sizeup/intel/engine.py _call, _split, _own_window, _handed): a second,
unrelated call that airs while one is open on the same talkgroup is an event of its own (another console, another
place), scored on its own rows; units alone never split one (a second car, an officer joining, a lookout); the same
place, shared units, "another caller" or a read-back still join; one console passing a call to another (a handoff tied
to the call) is one call, its town the console that took it. The Intel page and More info list the other calls in an
event's window (eventlog.py siblings), and the calls in it that are no event (console_calls). The #961 review's cases
are here as regression tests.

Every radio ID, label, agency, unit and address here is invented (this repository is public)."""

from __future__ import annotations

import pytest

from sizeup.intel import rules as rules_mod
from sizeup.intel.model import Place
from tests.test_intel import Clock, engine, row
from tests.test_intel_talkers import FakeRadios

A_ID, B_ID, OFFICER_ID = "7701", "7801", "7702"
LABELS = {A_ID: "Example PD dispatch", B_ID: "Testville PD dispatch", OFFICER_ID: "Example PD officer"}
TOWNS = {"Example PD": "Rye", "Testville PD": "Port Chester"}         # two of the police pack's towns (public)
PLACES = {"12 sample rd": (41.0, -73.66, "12 Sample Rd, Port Chester", "Port Chester"),
          "40 test rd": (41.02, -73.69, "40 Test Rd, Rye", "Rye")}
TEST_AVE = {"Rye": (41.03, -73.70), "Port Chester": (41.00, -73.66)}    # "9 Test Ave" in either town: the prior picks


def locate(text, scope):
    low = text.lower()
    for k, (lat, lon, label, town) in PLACES.items():
        if k in low:
            return Place("address", lat, lon, 25.0, label, town, True, 0.9)
    if "9 test ave" in low:                   # unconfirmed: the town is the talker's department's (place.py prefer)
        town = (scope.get("prefer") or ["Rye"])[0]
        return Place("address", *TEST_AVE[town], 40.0, f"9 Test Ave, {town}", town, False, 0.6)
    return None


def labelled(clock=None, radios=True):
    packs = rules_mod.load_packs(overrides={"intel_packs": {"police": {"agency_towns": TOWNS}}})
    e, sink = engine(packs=packs, locate=locate, clock=clock or Clock(1000))
    if radios:
        e.talkers.radios = FakeRadios(LABELS)
    return e, sink


def hear(e, tid, text, at, who=""):
    e.clock.t = at
    return e.feed(row(tid, text, at, radio_id=who))


def pushed(sink) -> list[str]:
    return [ev.category for ev, d in sink.pushes if d.would_push]


def opened(e) -> list:
    return sorted(e.events.all(), key=lambda x: x.id)


# -- a second call is its own event --------------------------------------------------------------------------------------

def test_two_calls_three_minutes_apart_another_console_place_and_units_are_two_events():
    e, sink = labelled()
    [a] = hear(e, "t1", "Car 3, can you respond to 12 Sample Rd, a male waving a machete outside the deli.", 1000, A_ID)
    [b] = hear(e, "t2", "Car 7, can you respond to 40 Test Rd, a male waving a knife in the parking lot.", 1180, B_ID)
    assert a.id != b.id and b.traffic_ids == ["t2"] and b.first_at == 1180
    assert (a.label_full, b.label_full) == ("12 Sample Rd, Port Chester", "40 Test Rd, Rye")
    assert pushed(sink) == ["weapons", "weapons"] and a.handoff == [] and b.handoff == []


def test_a_second_call_with_no_address_but_another_console_and_units_is_its_own_event():
    e, sink = labelled()
    [a] = hear(e, "t1", "Car 3, can you respond to 12 Sample Rd, a male waving a machete outside the deli.", 1000, A_ID)
    [b] = hear(e, "t2", "Car 7, can you respond, caller states a male waving a knife at the bus stop.", 1100, B_ID)
    assert a.id != b.id and b.precision == "none" and b.traffic_ids == ["t2"]
    assert e.events.get(a.id).traffic_ids == ["t1"] and pushed(sink) == ["weapons", "weapons"]


CALL = "Car 3, can you respond to 12 Sample Rd, a male waving a machete outside the deli."


@pytest.mark.parametrize("radios, who, text, at", [
    (False, "", "Car 7, can you respond, caller states a male waving a knife at the bus stop.", 1100),   # no labels
    (False, "", "Car 5 en route, be advised he's got a knife.", 1040),          # an officer joining (unlabelled)
    (True, OFFICER_ID, "Car 5 en route, be advised he's got a knife.", 1040),   # ... on a labelled officer radio
    (True, A_ID, "Car 5, can you respond, male waving a machete.", 1050),       # the same console's second car
    (True, A_ID, "Car 5, can you respond, male waving a machete.", 1200),       # ... after the call went quiet
    (True, A_ID, "Car 5 and car 6, be advised, male with a knife last seen heading toward the park.", 1180),  # lookout
])
def test_units_alone_never_split_a_call(radios, who, text, at):
    """#961 review (blocker): other units alone are one call's second car, an officer joining, a lookout to other cars:
    never a second event and a duplicate push (main joined them; the first version of #961 split them)."""
    e, sink = labelled(radios=radios)
    hear(e, "t1", CALL, 1000, A_ID if radios else "")
    hear(e, "t2", text, at, who)
    assert len(opened(e)) == 1 and pushed(sink) == ["weapons"]


def test_a_held_unplaced_call_takes_its_address_from_a_later_dispatch():
    """#961 review: a held unplaced fire_call ("location unknown") takes the address said 200 s later (as main)."""
    e, sink = labelled(radios=False)
    hear(e, "t1", "Car 3, respond, caller reports a burning smell in the building, location unknown.", 1000)
    [a] = opened(e)
    assert a.suppressed == "unplaced"
    hear(e, "t2", "Can you respond to 12 Sample Rd for the burning smell.", 1200)
    [a] = opened(e)
    assert a.label_full == "12 Sample Rd, Port Chester" and a.traffic_ids == ["t1", "t2"]


def test_a_re_feed_or_a_corrected_text_stays_in_its_own_event():
    """#961 review: after a restart (the window empty) a row already in an open event stays in it, whatever comes first."""
    from tests.test_intel import engine as fresh_engine
    second = "Car 7, can you respond, caller states a male waving a knife at the bus stop."
    e, sink = labelled()
    hear(e, "t1", CALL, 1000, A_ID)
    hear(e, "t2", second, 1100, B_ID)
    packs = rules_mod.load_packs(overrides={"intel_packs": {"police": {"agency_towns": TOWNS}}})
    for order in (["t2", "t1"], ["t2*"]):
        e2, _sink2 = fresh_engine(packs=packs, locate=locate, clock=Clock(1300))
        e2.events = e.events
        e2.talkers.radios = FakeRadios(LABELS)
        for t in order:
            if t.startswith("t2"):
                e2.feed(row("t2", second + (" now" if t.endswith("*") else ""), 1100, radio_id=B_ID))
            else:
                e2.feed(row("t1", CALL, 1000, radio_id=A_ID))
        assert [x.traffic_ids for x in opened(e2)] == [["t1"], ["t2"]]


def test_late_rows_after_a_split_go_to_their_own_call():
    e, sink = labelled()
    hear(e, "t1", CALL, 1000, A_ID)
    hear(e, "t2", "Car 7, can you respond, caller states a male waving a knife at the bus stop.", 1100, B_ID)
    e.clock.t = 1400
    e.feed(row("t3", "Car 7 copy, en route, he's got a knife.", 1095, late=True))
    e.feed(row("t4", "Car 3, be advised he's got a machete.", 1090, radio_id=A_ID, late=True))
    assert [x.traffic_ids for x in opened(e)] == [["t1", "t4"], ["t2", "t3"]]


def test_a_new_calls_score_is_its_own_rows_never_the_open_events():
    """An assault is under the threshold alone and over it with the dispatch cue: another console's assault with no
    cue never borrows the open assault's call-out to reach it."""
    e, sink = labelled()
    [a] = hear(e, "t1", "Car 3, can you respond to 12 Sample Rd, a male was punched outside the deli.", 1000, A_ID)
    assert a.category == "assault"
    out = hear(e, "t2", "Car 7, a male was punched in the lobby.", 1060, B_ID)
    assert out == [] and [x.id for x in opened(e)] == [a.id]
    assert e.events.get(a.id).traffic_ids == ["t1"] and pushed(sink) == ["assault"]


# -- more of the same call still joins ---------------------------------------------------------------------------------

def test_another_caller_at_the_same_place_from_any_console_or_saying_so_on_the_same_one_joins():
    e, sink = labelled()
    [a] = hear(e, "t1", "Car 3, can you respond to 12 Sample Rd, a male waving a machete outside the deli.", 1000, A_ID)
    [b] = hear(e, "t2", "Another caller, a male waving a machete at 12 Sample Rd.", 1060, B_ID)       # the same place
    [c] = hear(e, "t3", "Car 7, another caller, a male waving a machete, he is still there.", 1100, A_ID)
    assert a.id == b.id == c.id and c.traffic_ids == ["t1", "t2", "t3"] and pushed(sink) == ["weapons"]


@pytest.mark.parametrize("text", [
    "Car 7, can you respond to assist EMS, a male waving a knife at the bus stop.",       # "assist" is no tie
    "Car 7, another caller, a male waving a knife at the bus stop.",                       # nor a caller's words
])
def test_another_departments_console_beats_tie_words(text):
    """#961 review: "also", "assist", "as well", "another caller" never pull another department's call into this one."""
    e, sink = labelled()
    hear(e, "t1", CALL, 1000, A_ID)
    hear(e, "t2", text, 1100, B_ID)
    assert len(opened(e)) == 2 and pushed(sink) == ["weapons", "weapons"]


def test_a_read_back_joins():
    e, sink = labelled()
    [a] = hear(e, "t1", "Car 3, can you respond to 12 Sample Rd, a male waving a machete outside the deli.", 1000, A_ID)
    [b] = hear(e, "t2", "Car 3 copy, male waving a machete, en route.", 1030, OFFICER_ID)       # the call's unit
    [c] = hear(e, "t3", "Car 9 copy, 12 Sample Rd, male waving a machete.", 1060, OFFICER_ID)     # its place
    assert a.id == b.id == c.id and c.traffic_ids == ["t1", "t2", "t3"] and pushed(sink) == ["weapons"]


def test_a_new_call_said_so_is_still_a_new_incident():
    e, sink = labelled(radios=False)
    [a] = hear(e, "t1", "Car 3, can you respond to 12 Sample Rd, a male waving a machete outside the deli.", 1000)
    [b] = hear(e, "t2", "New call, car 3, a male waving a knife at the bus stop.", 1060)
    assert a.id != b.id


# -- a handoff between consoles is one call --------------------------------------------------------------------------------

def handoff(relay: str, place: str = "the gas station on the corner", ack: str = "Copy Example, we'll send one.",
            at: float = 1060):
    e, sink = labelled()
    out = hear(e, "t1", f"{relay} {place}, a male waving a machete.", 1000, A_ID)
    hear(e, "t2", ack, 1030, B_ID)
    out += hear(e, "t3", f"Car 7, can you respond to {place}, a male waving a machete.", at, B_ID)
    return e, sink, out


def test_a_handoff_from_one_console_to_another_is_one_call_in_the_town_that_took_it():
    e, sink, out = handoff("Testville, can you send a car to")
    [ev] = opened(e)
    assert {x.id for x in out} == {ev.id} and ev.traffic_ids == ["t1", "t3"] and pushed(sink) == ["weapons"]
    assert ev.handoff == ["Example PD dispatch", "Testville PD dispatch", "Port Chester"] and ev.town == "Port Chester"
    assert ev.precision == "none"


def test_a_handoff_reads_an_unconfirmed_place_again_in_the_town_that_took_it():
    e, sink, out = handoff("Testville, can you send a car to", "9 Test Ave")
    [ev] = opened(e)
    assert ev.handoff == ["Example PD dispatch", "Testville PD dispatch", "Port Chester"]
    assert (ev.label_full, ev.town, ev.confirmed) == ("9 Test Ave, Port Chester", "Port Chester", False)
    assert pushed(sink) == ["weapons"]


def test_without_a_relay_another_departments_dispatch_is_another_call():
    e, sink, out = handoff("Units, can you respond to", ack="Copy.")
    assert len(opened(e)) == 2 and all(x.handoff == [] for x in opened(e))
    assert pushed(sink) == ["weapons", "weapons"]


def test_a_relay_about_another_matter_is_no_handoff():
    """#961 review: a relay must be this call's (one of its rows, or naming its words, place or units)."""
    e, sink = labelled()
    hear(e, "t1", "Car 3, can you respond, a male waving a machete by the park.", 1000, A_ID)
    hear(e, "t2", "Testville, can you send a car for the traffic detail on the parade.", 1200, A_ID)
    hear(e, "t3", "Car 7, can you respond, a male waving a knife at the bus stop.", 1500, B_ID)
    assert len(opened(e)) == 2 and all(x.handoff == [] for x in opened(e)) and opened(e)[0].town == ""


def test_a_relay_long_before_the_other_consoles_dispatch_is_no_handoff():
    e, sink, out = handoff("Testville, can you send a car to", ack="Copy.", at=1900)
    assert len(opened(e)) == 2 and pushed(sink) == ["weapons", "weapons"]


@pytest.mark.parametrize("text, town, towns, said", [
    ("Rye, can you send a car", "Rye", ["Rye", "Rye Brook"], True),
    ("Rye Brook, can you send a car", "Rye", ["Rye", "Rye Brook"], False),       # never inside a longer town's name
    ("Rye Brook, can you send a car", "Rye Brook", ["Rye", "Rye Brook"], True),
    ("Testville, can you send a car", "", [], True),                            # the department's own name
])
def test_a_relay_names_the_other_side_by_its_town_or_its_department(text, town, towns, said):
    from sizeup.intel.engine import _names_side
    assert _names_side(text, "Testville PD dispatch", town, towns) is said


def test_the_handoff_is_kept_in_the_store(tmp_path):
    from sizeup.intel.events import SqlEvents
    from sizeup.intel.model import Event
    from tests.test_cad import make_cad
    cad = make_cad(tmp_path)
    ev = SqlEvents(cad.store).save(Event(pack="police", category="weapons", subject="s", first_at=1.0, last_at=1.0,
                                         until=2.0, handoff=["Example PD dispatch", "Testville PD dispatch", "Rye"]))
    assert SqlEvents(cad.store).get(ev.id).handoff == ["Example PD dispatch", "Testville PD dispatch", "Rye"]


# -- the other calls in an event's window: the Intel page and More info ---------------------------------------------------

def test_the_list_and_more_info_name_the_other_calls_in_the_window(tmp_path):
    from sizeup.cad.api import CadApi
    from sizeup.intel import eventlog
    from tests.test_intel import KEY
    from tests.test_intel_page import T0, get, listing, save
    from tests.test_intel import Clock as IntelClock
    from tests.test_cad import make_cad
    from tests.test_roles import VAL, people
    cad = make_cad(tmp_path, clock=IntelClock(T0 + 7200))
    people(cad)
    span = eventlog.SIBLING_S
    a = save(cad, first_at=T0, last_at=T0 + 60)
    b = save(cad, category="assault", first_at=T0 + 180, last_at=T0 + 200)
    far = save(cad, first_at=T0 + 60 + span + 5, last_at=T0 + span + 120)          # past a's window (b's within)
    other = save(cad, first_at=T0 + 30, last_at=T0 + 30, source_key=KEY.replace("9211", "9213"))   # another source
    rows = {r["id"]: r for r in listing(cad)["events"]}
    sib = lambda i: [x["id"] for x in rows[i]["siblings"]]                        # noqa: E731
    assert sib(a.id) == [b.id] and sib(b.id) == [a.id, far.id] and sib(far.id) == [b.id] and sib(other.id) == []
    assert rows[a.id]["siblings"][0] == {"id": b.id, "at": T0 + 180, "category": "assault",
                                         "label": rows[b.id]["label"]}
    code, d = get(CadApi(cad), f"/api/cad/prealerts/intel/{a.id}", VAL)
    assert code == 200 and [x["id"] for x in d["siblings"]["items"]] == [b.id] and d["siblings"]["window_s"] == span


def test_the_page_row_and_more_info_link_the_other_calls():
    from tests.test_intel_page import NODE, STATIC, run_node
    if NODE is None:
        pytest.skip("node not installed")
    ev = {"id": 5, "at": 1000, "last_at": 1060, "status": "open", "pack_label": "PD", "category": "weapons",
          "label": "Weapon", "level": "push", "would": "", "what": 0.7, "score_level": "high", "parts": [],
          "decided": "rules", "source_key": "k", "source": "PD", "system": "mrrs", "place": "", "precision": "none",
          "confirmed": False, "town": "", "lat": None, "lon": None, "radius_m": 0, "held": None, "pushed_at": 1000,
          "n": 1, "votes": {}, "mine": [], "notes": {"n": 0, "tags": [], "review": 0},
          "siblings": [{"id": 6, "at": 1100, "category": "assault", "label": "Assault"},
                       {"id": 7, "at": 1200, "category": "edp", "label": "EDP"}],
          "calls": [{"at": 1300, "n": 3, "consoles": ["Example PD dispatch", "Testville PD dispatch"]}]}
    script = f"""
{(STATIC / "intel-page.js").read_text()}
IntelPage.render({{ events: [IN], total: 1, page: 0, size: 50, since: 0, until: 2000, now: 2000, facets: {{}} }});
const t = $("#il-rows").children[0].children[0];
const sib = t.find("il-sibs")[0];
const links = sib.find("il-sib").map((a) => [a.textContent, a.href]);
console.log(JSON.stringify({{ text: sib.textContent, links, title: sib.title }}));
"""
    out = run_node(script, ev)
    assert out["text"].startswith("3 other calls ") and "Example PD dispatch → Testville PD dispatch" in out["title"] and out["links"] == [["#6", "#prealert/intel/6"],
                                                                         ["#7", "#prealert/intel/7"]]
    js = (STATIC / "prealert-info.js").read_text()
    assert 'other call${n === 1 ? "" : "s"} in this window' in js and '"pi-around": (d) => [d.before_after, d.siblings]' in js


# -- a call no rule raised is still another call in the window -------------------------------------------------------------

def test_console_calls_are_the_consoles_transmissions_in_no_event_and_not_an_events_conversation():
    from sizeup.intel import talkers as T
    from sizeup.intel.eventlog import console_calls
    known = {rid: T.parse(label) for rid, label in LABELS.items()}
    tx = lambda i, at, rid: {"id": i, "start": at, "system": "mrrs", "radio_id": rid}         # noqa: E731
    txs = [tx("e1", 1000, A_ID), tx("x0", 1030, A_ID),                    # an event, and its console just after it
           tx("h1", 1220, A_ID), tx("h2", 1225, B_ID), tx("o1", 1240, OFFICER_ID), tx("h3", 1263, B_ID),
           tx("x1", 1521, A_ID), tx("e2", 1550, A_ID)]                    # another event's conversation
    out = console_calls(txs, {"e1", "e2"}, lambda t: known.get(t["radio_id"]), 120)
    assert out == [{"at": 1220, "n": 3, "consoles": ["Example PD dispatch", "Testville PD dispatch"]}]
    assert console_calls(txs, {"e1", "e2"}, lambda t: None, 120) == []    # no labels: none, as before
    short = [dict(t, words=1) for t in txs]                               # "yeah", "copy": no call
    assert console_calls(short, {"e1", "e2"}, lambda t: known.get(t["radio_id"]), 120) == []


def test_the_list_and_more_info_count_a_call_that_is_no_event(tmp_path):
    from sizeup.cad.api import CadApi
    from tests.test_cad import make_cad
    from tests.test_intel import Clock as IntelClock
    from tests.test_intel import ctx_row
    from tests.test_intel_page import T0, get, listing, save
    from tests.test_roles import VAL, people
    cad = make_cad(tmp_path, clock=IntelClock(T0 + 7200))
    people(cad)
    cad.intel.talkers.radios = FakeRadios(LABELS)
    a = save(cad, first_at=T0, last_at=T0 + 60, traffic_ids=["c1-t0"])
    cad.comms.record({**ctx_row("c1-t0", "invented", T0), "radio_id": A_ID})
    for i, (at, rid) in enumerate([(T0 + 300, A_ID), (T0 + 310, B_ID), (T0 + 330, B_ID)]):
        cad.comms.record({**ctx_row(f"c2-t{i}", "car 7 respond to the invented place", at), "radio_id": rid})
    [row_] = [r for r in listing(cad)["events"] if r["id"] == a.id]
    assert row_["siblings"] == [] and row_["calls"] == [
        {"at": T0 + 300, "n": 3, "consoles": ["Example PD dispatch", "Testville PD dispatch"]}]
    code, d = get(CadApi(cad), f"/api/cad/prealerts/intel/{a.id}", VAL)
    assert code == 200 and d["siblings"]["calls"] == row_["calls"] and d["siblings"]["window_s"] == 600
