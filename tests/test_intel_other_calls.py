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
    assert out["text"].startswith("3 other calls ") and "Example PD dispatch · Testville PD dispatch · 3 console tx" in out["title"] and out["links"] == [["#6", "#prealert/intel/6"],
                                                                         ["#7", "#prealert/intel/7"]]
    js = (STATIC / "prealert-info.js").read_text()
    assert 'other call${n === 1 ? "" : "s"} in this window' in js and '"pi-around": (d) => [d.before_after, d.siblings]' in js


# -- a call no rule raised is still another call in the window -------------------------------------------------------------

def test_console_calls_are_the_consoles_transmissions_in_no_event_and_not_an_events_conversation():
    from sizeup.intel import talkers as T
    from sizeup.intel.eventlog import console_calls
    known = {rid: T.parse(label) for rid, label in LABELS.items()}
    tx = lambda i, at, rid: {"id": i, "start": at, "system": "mrrs", "radio_id": rid}         # noqa: E731
    txs = [tx("e1", 1000, A_ID), tx("x0", 1020, A_ID),                    # an event, and its console's answer
           tx("h1", 1220, A_ID), tx("h2", 1225, B_ID), tx("o1", 1240, OFFICER_ID), tx("h3", 1263, B_ID),
           dict(tx("x1", 1521, A_ID), words=2), tx("e2", 1550, A_ID)]     # a callsign alone, then another event
    out = console_calls(txs, {"e1", "e2"}, lambda t: known.get(t["radio_id"]), 120)
    assert out == [{"at": 1220, "n": 3, "consoles": ["Example PD dispatch", "Testville PD dispatch"]}]
    assert console_calls(txs, {"e1", "e2"}, lambda t: None, 120) == []    # no labels: none, as before
    short = [dict(t, words=1) for t in txs]                               # "yeah", "copy": no call
    assert console_calls(short, {"e1", "e2"}, lambda t: known.get(t["radio_id"]), 120) == []


def of_label(t):
    from sizeup.intel import talkers as T
    return T.parse(LABELS[t["radio_id"]]) if t["radio_id"] in LABELS else None


def tx_(i, at, rid, **kw):
    return {"id": i, "start": at, "system": "mrrs", "radio_id": rid, **kw}


def test_another_departments_call_right_after_an_event_shows():
    """#961 re-review: the event's call-out at 1000, another department's console dispatching another call at 1090."""
    from sizeup.intel.eventlog import console_calls
    out = console_calls([tx_("e1", 1000, A_ID), tx_("c1", 1090, B_ID), tx_("c2", 1100, B_ID)], {"e1"}, of_label, 120)
    assert out == [{"at": 1090, "n": 2, "consoles": ["Testville PD dispatch"]}]


def test_a_busy_console_chain_is_split_into_calls_and_never_dropped_whole():
    """#961 re-review: console traffic every 100 s for 15 min with an event's row in the middle."""
    from sizeup.intel.eventlog import console_calls
    txs = [tx_(f"c{i}", 400 + 100 * i, A_ID if i % 2 else B_ID) for i in range(16)]
    txs.insert(6, tx_("e1", 1000, A_ID))
    out = console_calls(txs, {"e1"}, of_label, 120)
    assert out and all(c["n"] <= 2 for c in out) and sum(c["n"] for c in out) >= 14       # (only e1's answer left out)


def test_a_third_console_starts_another_call_and_an_answer_is_its_events():
    from sizeup.intel.eventlog import console_calls
    labels = {**LABELS, "7901": "Sampleton PD dispatch"}
    from sizeup.intel import talkers as T
    of = lambda t: T.parse(labels[t["radio_id"]]) if t["radio_id"] in labels else None     # noqa: E731
    txs = [tx_("e1", 1000, OFFICER_ID), tx_("a1", 1010, A_ID),           # a unit's report, its console's answer
           tx_("p1", 1300, A_ID), tx_("p2", 1310, B_ID), tx_("p3", 1320, "7901")]
    out = console_calls(txs, {"e1"}, of, 120)
    assert [(c["at"], c["n"]) for c in out] == [(1300, 2), (1320, 1)]


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


def test_a_tie_by_talker_is_by_radio_id_never_the_shared_talker_text():
    """Every unlabelled radio of another system is stored with one talker text: two of them are two talkers. A row ties
    to the call of its own radio, never to another radio's call that shares the text."""
    e, sink = labelled(radios=False)
    shared = {"talker": "unknown MRRS radio"}
    e.clock.t = 1000
    [a] = e.feed(row("t1", "Male waving a machete at 12 Sample Rd.", 1000, radio_id="7703", **shared))
    e.clock.t = 1030
    [b] = e.feed(row("t2", "Male waving a machete at 40 Test Rd.", 1030, radio_id="7704", **shared))
    e.clock.t = 1060
    [c] = e.feed(row("t3", "Be advised, male waving a machete, he's on the porch.", 1060, radio_id="7704", **shared))
    assert a.id != b.id and c.id == b.id and c.traffic_ids == ["t2", "t3"]


def test_rows_with_no_radio_id_never_tie_on_a_generic_talker_text():
    from sizeup.intel.engine import _talker_ties
    assert _talker_ties({"system": "mrrs", "radio_id": "7703,7704"}, "unknown MRRS radio") == {"mrrs:7703", "mrrs:7704"}
    assert _talker_ties({"system": "mrrs", "radio_id": ""}, "unknown MRRS radio") == frozenset()
    assert _talker_ties({"system": "mrrs", "radio_id": ""}, "CAR 3") == {"CAR 3"}
    assert _talker_ties({"system": "mrrs", "radio_id": "-1"}, "unknown MRRS radio") == frozenset()   # (#980: undecoded)


@pytest.mark.parametrize("txs", [
    [tx_("e1", 1000, A_ID), tx_("c1", 1060, A_ID), tx_("c2", 1075, A_ID)],                          # after a pause
    [tx_("e1", 1000, A_ID), tx_("o1", 1030, OFFICER_ID), tx_("c1", 1060, A_ID), tx_("o2", 1070, OFFICER_ID)],
])
def test_the_same_consoles_next_call_after_a_pause_shows(txs):
    """#961 re-review: an event's conversation is only the chain of adjacent transmissions CHAIN_S apart."""
    from sizeup.intel.eventlog import console_calls
    assert console_calls(txs, {"e1"}, of_label, 120) != []


def test_an_events_call_up_and_immediate_follow_up_stay_hidden():
    from sizeup.intel.eventlog import console_calls
    txs = [tx_("u1", 980, A_ID), tx_("o0", 990, OFFICER_ID), tx_("e1", 1000, A_ID), tx_("o1", 1015, OFFICER_ID),
           tx_("f1", 1030, A_ID)]
    assert console_calls(txs, {"e1"}, of_label, 120) == []


def long_event_site(tmp_path):
    from tests.test_cad import make_cad
    from tests.test_intel import Clock as IntelClock
    from tests.test_intel import ctx_row
    from tests.test_intel_page import T0, save
    from tests.test_roles import people
    cad = make_cad(tmp_path, clock=IntelClock(T0 + 3 * 3600))
    people(cad)
    cad.intel.talkers.radios = FakeRadios(LABELS)
    long_ = save(cad, first_at=T0 - 300, last_at=T0 + 2 * 3600, traffic_ids=["L1", "L2"])     # a two-hour call
    a = save(cad, category="assault", first_at=T0, last_at=T0 + 30, traffic_ids=["A1"])
    cad.comms.record({**ctx_row("A1", "invented words for the event row", T0), "radio_id": A_ID})
    cad.comms.record({**ctx_row("L2", "invented words for the long call update", T0 + 400), "radio_id": B_ID})
    return cad, long_, a


def test_a_long_event_is_a_sibling_and_its_rows_are_no_calls(tmp_path):
    """#961 re-review: an event overlapping the window however long after it ends (a two-hour call)."""
    from sizeup.cad.api import CadApi
    from tests.test_intel_page import get, listing
    from tests.test_roles import VAL
    cad, long_, a = long_event_site(tmp_path)
    [r] = [x for x in listing(cad)["events"] if x["id"] == a.id]
    assert [s["id"] for s in r["siblings"]] == [long_.id] and r["calls"] == [] and "truncated" not in r
    code, d = get(CadApi(cad), f"/api/cad/prealerts/intel/{a.id}", VAL)
    assert [s["id"] for s in d["siblings"]["items"]] == [long_.id] and d["siblings"]["calls"] == []


def test_a_span_is_read_in_pages_and_past_the_cap_says_truncated(tmp_path, monkeypatch):
    from sizeup.intel import eventlog
    from tests.test_intel import ctx_row
    from tests.test_intel_page import T0, listing
    cad, long_, a = long_event_site(tmp_path)
    for i in range(7):                                    # another department's call, one tx every 20 s
        cad.comms.record({**ctx_row(f"P{i}", "car 7 respond to the invented place", T0 + 200 + 20 * i), "radio_id": B_ID})
    monkeypatch.setattr(eventlog, "TX_PAGE", 2)
    [r] = [x for x in listing(cad)["events"] if x["id"] == a.id]
    assert [c["n"] for c in r["calls"]] == [7] and "truncated" not in r        # four pages, nothing lost
    monkeypatch.setattr(eventlog, "TX_MAX", 4)
    [r] = [x for x in listing(cad)["events"] if x["id"] == a.id]
    assert r["truncated"] is True


def test_units_chatter_never_hides_the_consoles_next_call():
    """#961 re-review: an event's conversation chain stops CALL_SPAN_S from its row."""
    from sizeup.intel.eventlog import console_calls
    txs = [tx_("e1", 1000, A_ID)] + [tx_(f"o{i}", 1020 + 20 * i, OFFICER_ID) for i in range(14)]
    txs += [tx_("c1", 1300, A_ID), tx_("c2", 1310, A_ID)]
    assert console_calls(txs, {"e1"}, of_label, 120) == [{"at": 1300, "n": 2, "consoles": ["Example PD dispatch"]}]
