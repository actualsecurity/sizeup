"""Another call inside an open one's window (sizeup/intel/engine.py _call, _split, _own_window, _handed): a second,
unrelated call that airs while one is open on the same talkgroup is an event of its own (another console, another
place, other units, a second assignment), scored on its own rows; the same place, shared units, "another caller" or a
read-back still join; one console passing a call to another (a handoff) is one call, its town the console that took
it. The Intel page and More info list the other calls in an event's window (eventlog.py siblings).

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
ELM = {"Rye": (41.03, -73.70), "Port Chester": (41.00, -73.66)}         # "9 Elm St" in either town: the prior picks


def locate(text, scope):
    low = text.lower()
    for k, (lat, lon, label, town) in PLACES.items():
        if k in low:
            return Place("address", lat, lon, 25.0, label, town, True, 0.9)
    if "9 elm st" in low:                     # unconfirmed: the town is the talker's department's (place.py prefer)
        town = (scope.get("prefer") or ["Rye"])[0]
        return Place("address", *ELM[town], 40.0, f"9 Elm St, {town}", town, False, 0.6)
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


def test_other_units_alone_split_with_no_radio_labels():
    e, sink = labelled(radios=False)
    [a] = hear(e, "t1", "Car 3, can you respond to 12 Sample Rd, a male waving a machete outside the deli.", 1000)
    [b] = hear(e, "t2", "Car 7, can you respond, caller states a male waving a knife at the bus stop.", 1100)
    assert a.id != b.id and pushed(sink) == ["weapons", "weapons"]


def test_a_second_assignment_after_the_first_went_quiet_is_a_new_call_but_not_within_the_window():
    first = "Can you respond to 12 Sample Rd, a male waving a machete outside the deli."
    second = "Car 7, can you respond, a male waving a knife at the bus stop."
    e, sink = labelled(radios=False)
    [a] = hear(e, "t1", first, 1000)
    [b] = hear(e, "t2", second, 1200)                  # over window_s after the call: a second assignment
    assert a.id != b.id and pushed(sink) == ["weapons", "weapons"]
    e, sink = labelled(radios=False)
    [a] = hear(e, "t1", first, 1000)
    [b] = hear(e, "t2", second, 1090)                  # within it: the unit sent to this call
    assert a.id == b.id and pushed(sink) == ["weapons"]


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

def test_another_caller_at_the_same_place_or_saying_so_joins_whatever_the_console():
    e, sink = labelled()
    [a] = hear(e, "t1", "Car 3, can you respond to 12 Sample Rd, a male waving a machete outside the deli.", 1000, A_ID)
    [b] = hear(e, "t2", "Another caller, a male waving a machete at 12 Sample Rd.", 1060, B_ID)
    [c] = hear(e, "t3", "Car 7, another caller, a male waving a machete, he is still there.", 1100, B_ID)
    assert a.id == b.id == c.id and c.traffic_ids == ["t1", "t2", "t3"] and pushed(sink) == ["weapons"]


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

def handoff(relay: str, place: str = "the gas station on the corner", ack: str = "Copy Example, we'll send one."):
    e, sink = labelled()
    out = hear(e, "t1", f"{relay} {place}, a male waving a machete.", 1000, A_ID)
    hear(e, "t2", ack, 1030, B_ID)
    out += hear(e, "t3", f"Car 7, can you respond to {place}, a male waving a machete.", 1060, B_ID)
    return e, sink, out


def test_a_handoff_from_one_console_to_another_is_one_call_in_the_town_that_took_it():
    e, sink, out = handoff("Testville, can you send a car to")
    [ev] = opened(e)
    assert {x.id for x in out} == {ev.id} and ev.traffic_ids == ["t1", "t3"] and pushed(sink) == ["weapons"]
    assert ev.handoff == ["Example PD dispatch", "Testville PD dispatch"] and ev.town == "Port Chester"
    assert ev.precision == "none"


def test_a_handoff_reads_an_unconfirmed_place_again_in_the_town_that_took_it():
    e, sink, out = handoff("Testville, can you send a car to", "9 Elm St")
    [ev] = opened(e)
    assert ev.handoff == ["Example PD dispatch", "Testville PD dispatch"]
    assert (ev.label_full, ev.town, ev.confirmed) == ("9 Elm St, Port Chester", "Port Chester", False)
    assert pushed(sink) == ["weapons"]


def test_without_a_relay_another_departments_dispatch_is_another_call():
    e, sink, out = handoff("Units, can you respond to", ack="Copy.")
    assert len(opened(e)) == 2 and all(x.handoff == [] for x in opened(e))
    assert pushed(sink) == ["weapons", "weapons"]


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
                                         until=2.0, handoff=["Example PD dispatch", "Testville PD dispatch"]))
    assert SqlEvents(cad.store).get(ev.id).handoff == ["Example PD dispatch", "Testville PD dispatch"]


# -- the other calls in an event's window: the Intel page and More info ---------------------------------------------------

def test_the_list_and_more_info_name_the_other_calls_in_the_window(tmp_path):
    from sizeup.cad.api import CadApi
    from tests.test_intel import KEY
    from tests.test_intel_page import T0, get, listing, save
    from tests.test_intel import Clock as IntelClock
    from tests.test_cad import make_cad
    from tests.test_roles import VAL, people
    cad = make_cad(tmp_path, clock=IntelClock(T0 + 7200))
    people(cad)
    span = cad.intel._merge_span(cad.intel.pack("police"))
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
                       {"id": 7, "at": 1200, "category": "edp", "label": "EDP"}]}
    script = f"""
{(STATIC / "intel-page.js").read_text()}
IntelPage.render({{ events: [IN], total: 1, page: 0, size: 50, since: 0, until: 2000, now: 2000, facets: {{}} }});
const t = $("#il-rows").children[0].children[0];
const sib = t.find("il-sibs")[0];
const links = sib.find("il-sib").map((a) => [a.textContent, a.href]);
console.log(JSON.stringify({{ text: sib.textContent, links }}));
"""
    out = run_node(script, ev)
    assert out["text"].startswith("2 other calls ") and out["links"] == [["#6", "#prealert/intel/6"],
                                                                         ["#7", "#prealert/intel/7"]]
    js = (STATIC / "prealert-info.js").read_text()
    assert "other call${items.length === 1 ? \"\" : \"s\"} in this window" in js and '"pi-around": (d) => [d.before_after, d.siblings]' in js
