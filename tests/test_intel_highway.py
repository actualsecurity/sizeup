"""A crash on a highway by our district, heard first on another agency's channel (data/intel/highway.json,
sizeup/intel/highway.py, the engine's "highway": true category).

Held here: the words as speech-to-text garbles them (route numbers and exits said as words, a spoken mile marker,
"guard rail"); a crash at an exit by our district pushes once ("PD · Crash · I-998 EB near exit 40 (Sampleton PD)"), one
far down the road never does; the route alone pushes only from a source whose own town has that route by our district;
a second agency's report of the same crash joins its event (one push); our own call there links it, before (no push)
or after (no more pushes); and its source is promoted for speech-to-text for promote_min.

Invented geography only (this repository is public): the routes I-998 / I-997, their exits and markers, the towns
Sampleton and Faraway, and their coordinates are made up; the talkgroup numbers 9901-9903 are not in any file."""

from __future__ import annotations

import json
import math

import pytest

from sizeup.intel import highway as hw
from sizeup.intel import push as push_mod
from sizeup.intel import rules as rules_mod
from sizeup.intel.engine import IntelEngine, RecordingSink
from sizeup.intel.events import MemoryEvents
from tests.test_intel import Clock

LON = -74.50
DISTRICT = (40.50, 40.52, -74.51, -74.49)          # lat lo, lat hi, lon lo, lon hi: "our district" (a box)
NEXT_DOOR = "p25:mrrs:9901"                          # Sampleton PD (its town has I-998 by our district)
FAR = "p25:mrrs:9902"                                # Faraway PD (its town doesn't)
DOT = "p25:westchester:9903"                         # a road crew's channel (no town)


def exit_(route, value, lat, lon=LON, muni="Sampleton", d="EB"):
    return {"kind": "exit", "route": route, "dir": d, "value": value, "lat": lat, "lon": lon, "muni": muni}


def mm(route, value, lat, lon=LON, muni="Sampleton", d="EB"):
    return {"kind": "milemarker", "route": route, "dir": d, "value": value, "lat": lat, "lon": lon, "muni": muni}


MARKERS = {
    "I-998": [exit_("I-998", "38", 40.47, muni="Faraway"), exit_("I-998", "39", 40.49), exit_("I-998", "40", 40.51),
              exit_("I-998", "40A", 40.512), exit_("I-998", "41", 40.53), exit_("I-998", "42", 40.56, muni="Faraway"),
              exit_("I-998", "43", 40.62, muni="Faraway"),
              mm("I-998", "11.0", 40.505), mm("I-998", "11.2", 40.508), mm("I-998", "14.0", 40.60, muni="Faraway")],
    "I-997": [exit_("I-997", "7", 40.511, -74.501, d="NB"), exit_("I-997", "9", 40.60, -74.45, muni="Faraway", d="NB")],
    "I-996": [exit_("I-996", "2", 40.80, -74.20, muni="Faraway"), exit_("I-996", "3", 40.82, -74.20, muni="Faraway")],
}


def area_m(lat, lon):
    lo, hi, w, e = DISTRICT
    dy = max(lo - lat, 0.0, lat - hi) * 110_574.0
    dx = max(w - lon, 0.0, lon - e) * 111_320.0 * math.cos(math.radians(lat))
    return math.hypot(dx, dy)


def corridor(buffer_m=2000.0):
    return hw.Corridor(MARKERS, area_m, buffer_m)


def pack_dict(**over):
    d = json.loads((rules_mod.DATA / "highway.json").read_text())
    d["scope"] = dict(d["scope"], sources=["p25:mrrs:*", "p25:westchester:*"])
    d["towns"] = {NEXT_DOOR: ["Sampleton"], FAR: ["Faraway"]}
    d.update(over)
    return d


def engine(clock=None, incidents=None, **over):
    sink = RecordingSink()
    e = IntelEngine(packs=rules_mod.load_packs([pack_dict(**over)]), settings={}, events=MemoryEvents(), sink=sink,
                    thread=False, clock=clock or Clock(10_000.0))
    e.highway = corridor()
    e.incidents = incidents
    return e, sink


def row(tid, text, at, source=NEXT_DOOR, dept="Sampleton PD", service="police"):
    return {"id": tid, "start": at, "end": at + 4, "source": source, "system": source.split(":")[1], "service": service,
            "dept": dept, "tier": "text", "talkgroup": int(source.rsplit(":", 1)[1]), "channel": "", "talker": "",
            "rig": "", "radio_id": "", "text": text, "alt_text": "", "model": "whisper"}


def pushed(sink):
    return [(ev, d) for ev, d in sink.pushes if d.would_push]


# -- the words ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,route,d,kind,value", [
    ("two cars into the guardrail on I-998 eastbound by exit 40", "I-998", "EB", "exit", "40"),
    ("crash on nine ninety eight west at exit forty a", "I-998", "WB", "exit", "40A"),
    ("the 998 east, mile marker eleven point two, rollover", "I-998", "EB", "mm", "11.2"),
    ("998 north bound by exit 39", "I-998", "NB", "exit", "39"),
    ("accident at the ramp from 998 to the 997", "I-998", "", "", ""),
])
def test_parse_garbled_route_tokens(text, route, d, kind, value):
    ref = hw.parse(text, MARKERS)
    assert ref is not None and (ref.route, ref.dir, ref.kind, ref.value) == (route, d, kind, value)


def test_a_bare_number_needs_a_highway_word():
    assert hw.parse("car 998 respond to 12 Sample Street", MARKERS) is None
    assert hw.parse("on the 998 by the overpass", MARKERS).route == "I-998"


def test_places_and_the_gate():
    c = corridor()
    at40 = c.place(hw.parse("998 eastbound exit 40", MARKERS))
    assert (at40.precision, at40.label, at40.town) == ("landmark", "I-998 EB exit 40", "Sampleton")
    assert c.gate(at40) == ""
    far = c.place(hw.parse("998 eastbound exit 43", MARKERS))
    assert c.gate(far) == "out_of_area"
    marker = c.place(hw.parse("998 mile marker 11.2", MARKERS))
    assert marker.label == "I-998 MM 11.2" and c.gate(marker) == ""
    ramp = c.place(hw.parse("crash on the ramp, 998 to 997", MARKERS))
    assert ramp.label == "I-998 / I-997 interchange" and c.gate(ramp) == ""
    route = c.place(hw.parse("on the 998 west", MARKERS))
    assert (route.precision, route.lat, route.label) == ("street", None, "I-998 WB")
    assert c.gate(route, ["Sampleton"]) == "" and c.gate(route, ["Faraway"]) == "unplaced" and c.gate(route) == "unplaced"
    assert c.gate(c.place(hw.parse("cross county eastbound", MARKERS))) == "out_of_area"   # a route we have no markers of


def test_buffer_is_configurable():
    p = corridor(buffer_m=500).place(hw.parse("998 exit 39", MARKERS))
    assert corridor(buffer_m=500).gate(p) == "out_of_area" and corridor(buffer_m=2000).gate(p) == ""


# -- the engine -----------------------------------------------------------------------------------------------------

def test_in_district_crash_pushes_once_with_its_title():
    clock = Clock(10_000.0)
    e, sink = engine(clock)
    e.feed(row("a", "Sampleton, we have two cars into the guardrail on I-998 eastbound by exit 40", clock.t))
    (ev, d), = pushed(sink)
    assert ev.category == "crash" and ev.label_full == "I-998 EB exit 40"
    assert d.message["title"] == "PD · Crash · I-998 EB near exit 40 (Sampleton PD)"
    assert d.message["body"].splitlines()[0] == "I-998 EB exit 40 · Sampleton"
    assert d.message["body"].splitlines()[1].split(" · ")[1] in ("medium", "high")
    clock.t += 30
    e.feed(row("b", "copy, two vehicles, eastbound by exit 40, one with injuries", clock.t))
    assert len(pushed(sink)) == 1                   # the follow-up joins it: one push per crash
    assert e.events.open_events()[0].traffic_ids == ["a", "b"]


def test_out_of_district_segment_never_pushes():
    clock = Clock(10_000.0)
    e, sink = engine(clock)
    e.feed(row("a", "rollover on 998 eastbound at exit 43, units responding", clock.t, source=FAR, dept="Faraway PD"))
    assert not pushed(sink)
    ev, = e.events.open_events()
    assert ev.suppressed == "out_of_area" and ev.mode == "log"


def test_route_alone_from_the_town_next_door_pushes_but_not_from_elsewhere():
    clock = Clock(10_000.0)
    e, sink = engine(clock)
    e.feed(row("a", "units for a four car, 998 westbound, left lanes blocked", clock.t))
    (ev, d), = pushed(sink)
    assert ev.label_full == "I-998 WB" and d.message["body"].splitlines()[0] == "I-998 WB · exit not heard"
    e2, sink2 = engine(Clock(10_000.0))
    e2.feed(row("a", "units for a four car, 998 westbound, left lanes blocked", 10_000.0, source=FAR, dept="Faraway PD"))
    assert not pushed(sink2) and e2.events.open_events()[0].suppressed == "unplaced"


def test_patrol_cars_are_not_a_crash():
    e, sink = engine()
    e.feed(row("a", "can I get two cars to the 998 eastbound exit 40 for a detail", 10_000.0))
    assert not sink.events and not pushed(sink)


def test_another_agency_reports_the_same_crash_one_event_one_push():
    clock = Clock(10_000.0)
    e, sink = engine(clock)
    e.feed(row("a", "two cars into the guardrail on I-998 eastbound by exit 40", clock.t))
    clock.t += 120
    e.feed(row("b", "crew advising a crash eastbound 998 at exit 41, debris in the travel lane", clock.t, source=DOT,
               dept="", service="other"))
    assert len(pushed(sink)) == 1
    ev, = e.events.open_events()
    assert ev.traffic_ids == ["a", "b"]


def test_a_later_dispatch_of_ours_links_it_and_stops_its_pushes():
    clock = Clock(10_000.0)
    incs: list[dict] = []
    e, sink = engine(clock, incidents=lambda now: list(incs))
    e.feed(row("a", "two cars into the guardrail on I-998 eastbound by exit 40", clock.t))
    assert len(pushed(sink)) == 1
    clock.t += 600
    incs.append({"id": 7, "lat": 40.509, "lon": -74.499, "label": "I-998 EB", "created_at": clock.t, "closed_at": None})
    e.tick(clock.t)
    ev, = e.events.open_events()
    assert (ev.near_incident, ev.note, ev.mode) == (7, "dispatched", "map")
    clock.t += 60
    e.feed(row("b", "update, rollover now, entrapment, 998 eastbound exit 40", clock.t))
    assert len(pushed(sink)) == 1                   # linked: no re-push


def test_our_call_already_there_holds_it():
    clock = Clock(10_000.0)
    incs = [{"id": 3, "lat": 40.51, "lon": -74.50, "label": "", "created_at": clock.t - 300, "closed_at": None}]
    e, sink = engine(clock, incidents=lambda now: incs)
    e.feed(row("a", "two cars into the guardrail on I-998 eastbound by exit 40", clock.t))
    assert not pushed(sink)
    ev, = e.events.open_events()
    assert (ev.suppressed, ev.near_incident) == ("near_incident", 3)


def test_the_source_is_promoted_for_a_while():
    clock = Clock(10_000.0)
    e, sink = engine(clock)
    calls = []
    e.on_promote = lambda: calls.append(1)
    e.feed(row("a", "two cars into the guardrail on I-998 eastbound by exit 40", clock.t))
    assert e.promoted_since(clock.t) == {NEXT_DOOR: 10_000.0}
    assert e.promoted_since(clock.t + 9 * 60) == {NEXT_DOOR: 10_000.0}
    assert e.promoted_since(clock.t + 11 * 60) == {}
    for _ in range(50):                             # (on its own thread)
        if calls:
            break
        import time
        time.sleep(0.01)
    assert calls


def test_a_county_source_is_promoted_by_its_queue_key():
    clock = Clock(10_000.0)
    e, _sink = engine(clock)
    e.feed(row("a", "rollover, 998 eastbound exit 40, two vehicles", clock.t, source=DOT, dept="", service="other"))
    assert e.promoted_since(clock.t) == {"tg:9903": 10_000.0}


def test_a_held_crash_never_promotes_its_channel():
    clock = Clock(10_000.0)
    e, sink = engine(clock)
    e.feed(row("a", "rollover, 998 eastbound, two vehicles, exit not known yet", clock.t, source=DOT, dept="",
               service="other"))
    assert not pushed(sink) and e.events.open_events()[0].suppressed == "unplaced"
    assert e.promoted_since(clock.t) == {}           # (never URGENT / AWS for a crash not known to be ours)


def test_no_promotion_out_of_district():
    clock = Clock(10_000.0)
    e, _sink = engine(clock)
    e.feed(row("a", "rollover on 998 eastbound at exit 43", clock.t, source=FAR, dept="Faraway PD"))
    assert e.promoted_since(clock.t) == {}


def test_without_a_corridor_nothing_pushes():
    e, sink = engine()
    e.highway = None
    e.feed(row("a", "two cars into the guardrail on I-998 eastbound by exit 40", 10_000.0))
    assert not pushed(sink)


def test_settings_tune_the_buffer():
    packs = rules_mod.load_packs([pack_dict()], {"intel_packs": {"highway": {"highway": {"buffer_m": 300}}}})
    assert packs[0].hw["buffer_m"] == 300
    with pytest.raises(ValueError):
        rules_mod.load_packs([pack_dict()], {"intel_packs": {"highway": {"highway": {"buffer_m": -1}}}})


def test_its_sources_are_not_push_sources_unless_settings_say_so():
    packs = rules_mod.load_packs([pack_dict()])
    assert packs[0].stt_high is False
    on = rules_mod.load_packs([pack_dict()], {"intel_packs": {"highway": {"stt_high": True}}})
    assert on[0].stt_high is True


def test_the_scrub_lets_a_highway_label_through():
    from sizeup.intel.model import Event
    ev = Event(id=1, pack="highway", category="crash", precision="landmark", lat=40.51, lon=-74.5,
               label_full="I-998 EB exit 40A", town="Sampleton", service="police", dept="Sampleton PD",
               first_at=10_000.0, level="medium", priority=4)
    msg = push_mod.compose(ev, "Radio", {"label": "Crash", "highway": True, "tag": "car"})
    assert msg["title"] == "PD · Crash · I-998 EB near exit 40A (Sampleton PD)"
    assert push_mod.scrub(msg) == ""


# -- the model behind the rules (sizeup/intel/backing.py): only rows with a highway word, and none without a worker ---

def model_engine(backend, clock=None):
    from sizeup.intel import backing as B
    e, sink = engine(clock)
    e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=e.clock, log=lambda m: None, backend=backend)
    return e, sink


def crash_answer(confidence=0.9):
    return {"category": "crash", "confidence": confidence, "in_progress": True, "weapon": "none",
            "deescalation": False, "address": None, "reason": "a crash on the highway"}


def test_the_model_backs_a_row_the_rules_left_on_a_highway():
    from sizeup.intel import backing as B
    b = B.FakeBackend(crash_answer())
    e, sink = model_engine(b)
    e.feed(row("a", "Sampleton to the units, got a mess on the 998 eastbound by exit 40, lanes are blocked", 10_000.0))
    assert len(b.asked) == 1
    ev, = e.events.open_events()
    assert ev.category == "crash" and any(str(r[0]).startswith("model.") for r in ev.rules)
    assert ev.mode == "map" and not pushed(sink)    # the model alone: on the map, never a push ("model_mode")


def test_the_model_is_never_asked_about_a_row_without_a_highway_word():
    from sizeup.intel import backing as B
    b = B.FakeBackend(crash_answer())
    e, _sink = model_engine(b)
    e.feed(row("a", "Sampleton to the units, check on the welfare of the caller at the deli on Sample Street", 10_000.0))
    assert b.asked == []


def test_no_worker_means_rules_only():
    from sizeup.intel import backing as B
    b = B.FakeBackend(crash_answer(), up=False)
    e, sink = model_engine(b)
    e.feed(row("a", "Sampleton to the units, got a mess on the 998 eastbound by exit 40, lanes are blocked", 10_000.0))
    assert b.asked == [] and not e.events.open_events()
    e.feed(row("b", "two cars into the guardrail on I-998 eastbound by exit 40", 10_010.0))
    assert len(pushed(sink)) == 1                   # the rules still push


# -- speech-to-text: the promotion reaches the station (sizeup/channels.py, the operating channels' path) ------------

def test_the_station_takes_another_systems_key_in_promote():
    from sizeup import channels
    got = channels.hub_aws({"ok": True, "promote": ["tg:301", "p25:mrrs:9901", "fm:FG1"],
                            "promote_since": {"p25:mrrs:9901": 10_000.0}})
    assert got["promote"] == ["tg:301", "p25:mrrs:9901"] and got["promote_since"] == {"p25:mrrs:9901": 10_000.0}


def test_the_app_promotes_what_intel_promotes():
    from sizeup.app import App

    class Stub:
        forwarding = lambda self: False      # noqa: E731
        promoted_keys = App.promoted_keys
        intel_promoted = App.intel_promoted
        _promo_cache = (0.0, frozenset())

    clock = Clock(10_000.0)
    e, _sink = engine(clock)
    e.feed(row("a", "two cars into the guardrail on I-998 eastbound by exit 40", clock.t))
    s = Stub()
    s.cad = type("C", (), {"intel": e, "opchan": None})()
    assert s.promoted_keys() == frozenset({NEXT_DOOR})


# -- what isn't a highway crash (review) ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "car accident, one lane blocked eastbound",       # "one" is no route (a local road's number)
    "units to route one by the diner",
    "unit 120 west on patrol",
    "mva at 998 North Main Street",                   # a house number and its street
    "mva with injuries at 12 Hutchinson Avenue",      # a street named for a parkway
])
def test_streets_and_numbers_are_not_highways(text):
    assert hw.parse(text, {**MARKERS, "US-1": [exit_("US-1", "1", 40.51)], "NY-120": [exit_("NY-120", "1", 40.51)]}) is None


def test_exit_then_the_article_is_not_an_exit_letter():
    ref = hw.parse("998 eastbound exit 40 a car is on fire", MARKERS)
    assert (ref.kind, ref.value) == ("exit", "40")


@pytest.mark.parametrize("text", [
    "trooper requesting ems to the 998 eastbound exit 40 for an aided, male having chest pains",
    "accident on 998 eastbound exit 40 is cleared, all lanes open",
    "two car mva 998 eastbound exit 40, no injuries, moved to the shoulder",
    "heavy traffic on the 998 eastbound by exit 40, use caution",
])
def test_not_a_crash_pre_alert(text):
    e, sink = engine()
    e.feed(row("a", text, 10_000.0))
    assert not pushed(sink), text


def test_a_crash_held_elsewhere_never_takes_a_crash_by_us():
    clock = Clock(10_000.0)
    e, sink = engine(clock)
    e.feed(row("a", "rollover on 998 eastbound at exit 43, units responding", clock.t, source=FAR, dept="Faraway PD"))
    clock.t += 60
    e.feed(row("b", "units for a four car, 998 eastbound, left lanes blocked", clock.t))
    (ev, _d), = pushed(sink)
    assert ev.traffic_ids == ["b"]


def test_the_other_carriageway_is_another_crash():
    clock = Clock(10_000.0)
    e, sink = engine(clock)
    e.feed(row("a", "two cars into the guardrail on I-998 eastbound by exit 40", clock.t))
    clock.t += 25 * 60
    e.feed(row("b", "rollover with entrapment, 998 westbound at exit 41", clock.t, source=DOT, dept="", service="other"))
    assert len(pushed(sink)) == 2 and len(e.events.open_events()) == 2


def test_the_models_own_hour_cap():
    from sizeup.intel import backing as B
    b = B.FakeBackend(crash_answer(0.2))
    clock = Clock(10_000.0)
    e, _sink = model_engine(b, clock)
    for i in range(25):
        clock.t += 60
        e.feed(row(f"r{i}", f"Sampleton to the units, slow traffic on the 998 eastbound by exit 40, item {i}", clock.t))
    assert len(b.asked) == 20                       # highway.json's model_hour_cap: the police lane's budget is apart


# -- Settings > Intel and the hub (review) --------------------------------------------------------------------------

def test_the_admin_sets_the_highway_numbers_and_stt_high(tmp_path):
    from sizeup.cad.api import CadApi
    from tests.test_cad import make_cad
    from tests.test_roles import CASEY, call, people
    cad = make_cad(tmp_path)
    people(cad)
    api = CadApi(cad)
    post = lambda b: call(api, "/api/cad/settings/intel", b, CASEY)[0]      # noqa: E731
    assert post({"action": "pack", "pack": "highway", "highway": {"buffer_m": 3000}, "stt_high": True}) == 200
    hwy = cad.intel.pack("highway")
    assert hwy.hw["buffer_m"] == 3000 and hwy.stt_high is True
    assert post({"action": "pack", "pack": "highway", "highway": {"buffer_m": -5}}) == 400
    assert post({"action": "pack", "pack": "highway", "stt_high": "false"}) == 400
    assert post({"action": "pack", "pack": "police", "stt_high": False}) == 400       # a highway pack's only
    assert post({"action": "pack", "pack": "police", "highway": {"buffer_m": 100}}) == 400


def test_the_hub_sends_another_systems_key_only_to_a_station_that_takes_it(tmp_path):
    from sizeup import channels
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path))
    try:
        hub.intel_promoted = lambda: {NEXT_DOOR: 10_000.0}
        hub.promoted_changed()
        assert NEXT_DOOR in hub.promoted_keys()
        hub.hub.status = {"channels": {"promote_ok": True}}
        assert "promote" not in hub.channel_desired()["aws"]          # a station from before: county keys only
        hub.hub.status = {"channels": {"promote_ok": True, "promote_p25_ok": True}}
        want = hub.channel_desired()
        assert want["aws"]["promote"] == [NEXT_DOOR] and want["aws"]["promote_since"] == {NEXT_DOOR: 10_000.0}
        got = channels.hub_aws(want["aws"])                           # the station keeps it as sent: the same sig
        assert got["promote"] == [NEXT_DOOR]
        assert want["sig"] == channels.sig(want["prefs"], want["aws"]["ok"], want["tones"], want["aws"]["reserve"],
                                           want["squelch"], None, got["promote"])
    finally:
        hub.stop()


# -- speech-to-text's side of a crash promotion (the radio session's review) ------------------------------------------

def _clip(name, start, late=False):
    from sizeup.incidents import Traffic
    t = Traffic(name, "x", start, start + 1.0, "")
    if late:
        t.late = True
    return t


def test_raise_channel_takes_the_context_and_high_classes_too_never_a_rerun_catchup_or_late_clip():
    from sizeup.stt import CATCHUP, CONTEXT, CONTEXT_FALLBACK, HIGH, LIVE, LOW, RERUN, URGENT, SttQueue
    clock = Clock(2_000.0)
    q = SttQueue(clock=clock)
    ch = NEXT_DOOR
    for prio, item in ((CONTEXT, _clip("ctx", 1100.0)), (CONTEXT_FALLBACK, _clip("fb", 1110.0)), (LOW, _clip("low", 1120.0)),
                       (LIVE, _clip("live", 1130.0)), (HIGH, _clip("high", 1140.0)), (RERUN, _clip("rerun", 1150.0)),
                       (CATCHUP, _clip("catch", 1160.0)), (CONTEXT, _clip("late", 1170.0, late=True)),
                       (CONTEXT, _clip("before", 900.0))):
        q.put((prio, None, item, None, ch))
    assert q.raise_channel(ch, HIGH, 1000.0) == 4               # (HIGH already: as it was)
    got = {e[2].id: e[0] for e in q._items}
    assert got == {"ctx": HIGH, "fb": HIGH, "low": HIGH, "live": HIGH, "high": HIGH, "rerun": RERUN, "catch": CATCHUP,
                   "late": CONTEXT, "before": CONTEXT}
    assert q.raise_channel(ch, URGENT, 1000.0) == 5             # #801's raise to URGENT takes HIGH too
    clock.t += 299                                              # raised into HIGH: its wait starts again (never shed for
    q2 = SttQueue(clock=clock)                                  # the minutes it waited as context)
    q2.put((CONTEXT, None, _clip("waited", 2_000.0), None, ch))
    clock.t += 400
    assert q2.raise_channel(ch, HIGH, 1000.0) == 1
    item = q2.get_nowait()
    assert item is not None and item[2].id == "waited"


def test_a_station_hears_a_crash_channel_at_high_through_set_channel_prefs(tmp_path):
    from sizeup.app import HIGH as A_HIGH, URGENT as A_URGENT
    from sizeup.cloud import sttinput
    from sizeup.incidents import Traffic
    from sizeup.stt import CONTEXT, LOW, SttQueue
    from tests.test_stt_input import offline_station
    st = offline_station(tmp_path)
    try:
        q = SttQueue(clock=Clock(2_000.0))
        st.stt.raise_channel = q.raise_channel                  # the real queue's raise (not started: nothing is taken)
        q.put((CONTEXT, None, _clip("ctx", 1_500.0), None, NEXT_DOOR))
        q.put((LOW, None, _clip("low", 1_600.0), None, NEXT_DOOR))
        q.put((LOW, None, _clip("old", 900.0), None, NEXT_DOOR))
        t = Traffic("9901-1-call_1-t0", "Sample PD", 0.0, 1.0, "", talkgroup=9901)
        word = {"ok": True, "why": "", "reserve": [], "promote": ["tg:4031", NEXT_DOOR],
                "promote_since": {NEXT_DOOR: 1_000.0, "tg:4031": 1_000.0}}
        st.set_channel_prefs({}, word)
        assert {e[2].id: e[0] for e in q._items} == {"ctx": A_HIGH, "low": A_HIGH, "old": LOW}
        assert st._stt_class(t, NEXT_DOOR) == A_HIGH and st._stt_class(t, "tg:4031") == A_URGENT
        assert st._stt_lane(t, NEXT_DOOR) == sttinput.PRE
        assert st.stt_engine(NEXT_DOOR) == "auto"                          # (promoted Auto · background: Auto · priority)
        assert st.channels_applied()["promote_p25_ok"] is True
        st.set_channel_prefs({}, {"ok": True, "why": "", "reserve": []})         # the window over: as before
        assert st._stt_class(t, NEXT_DOOR) not in (A_HIGH, A_URGENT) and st.stt_engine(NEXT_DOOR) != "aws"
    finally:
        st.link.stop()


def test_a_promotion_never_outlives_promote_max_min_however_often_it_is_renewed():
    clock = Clock(10_000.0)
    e, _sink = engine(clock)
    e.feed(row("a", "two cars into the guardrail on I-998 eastbound by exit 40", clock.t))
    for i in range(8):                                          # a report every 5 min renews it
        clock.t += 300
        e.feed(row(f"r{i}", f"update {i}, the crash is still blocking two lanes, 998 eastbound exit 40", clock.t))
    assert e.promoted_since(10_000.0 + 29 * 60) and e.promoted_since(10_000.0 + 31 * 60) == {}


def test_a_renewal_after_a_lapse_raises_only_from_lead_s_before_it():
    from sizeup.cad.opchannel import LEAD_S
    clock = Clock(10_000.0)
    e, _sink = engine(clock)
    e.feed(row("a", "two cars into the guardrail on I-998 eastbound by exit 40", clock.t))
    assert e.promoted_since(clock.t) == {NEXT_DOOR: 10_000.0}
    clock.t += 12 * 60
    assert e.promoted_since(clock.t) == {}                      # lapsed
    e.feed(row("b", "two cars, one overturned now, 998 eastbound exit 40", clock.t))
    assert e.promoted_since(clock.t) == {NEXT_DOOR: clock.t - LEAD_S}     # never the whole backlog again


def test_the_hub_keeps_our_incidents_keys_first_past_the_cap(tmp_path):
    from sizeup import channels
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path))
    try:
        many = {f"p25:mrrs:{9800 + i}": 10_000.0 for i in range(channels.AWS_KEYS_MAX + 4)}
        hub.intel_promoted = lambda: many
        hub.cad.opchan.promoted = lambda now=None: {4031}
        hub.cad.opchan.promoted_since = lambda now=None: {"tg:4031": 9_000.0}
        hub.promoted_changed()
        hub.hub.status = {"channels": {"promote_ok": True, "promote_p25_ok": True}}
        sent = hub.channel_desired()["aws"]["promote"]
        assert sent[0] == "tg:4031" and len(sent) == channels.AWS_KEYS_MAX
    finally:
        hub.stop()


def test_a_promoted_crash_channel_is_denied_aws_with_the_context_budget():
    from sizeup.app import App

    class Stub:
        _chan_ov: dict = {}
        cfg = type("C", (), {"stt_aws_channels": ()})()
        channel_pref = lambda self, k: {"engine": "local"}       # noqa: E731
        promoted_keys = lambda self: frozenset({NEXT_DOOR, "tg:4031"})   # noqa: E731
        context_aws_keys = App.context_aws_keys
    assert Stub().context_aws_keys() == [NEXT_DOOR]             # (ours never: their own budget, as before)


def test_a_new_crash_on_a_channel_promoted_before_raises_its_own_backlog():
    clock = Clock(10_000.0)
    e, sink = engine(clock)
    e.feed(row("a", "two cars into the guardrail on I-998 eastbound by exit 40", clock.t))
    clock.t += 12 * 60
    assert e.promoted_since(clock.t) == {}                      # crash A's promotion lapsed
    first_b = clock.t - 300                                     # crash B, its row transcribed late
    e.feed(row("b", "rollover with entrapment, 998 westbound at exit 41", first_b))
    assert len(pushed(sink)) == 2
    assert e.promoted_since(clock.t) == {NEXT_DOOR: first_b}    # B's own backlog, never only LEAD_S before now


def _take(q, now):
    sheds, skips = [], []
    got = q._take(now, skips, sheds)
    return (got[2].id if got else None), [i.id for i, _w in sheds]


def test_a_clip_raised_into_high_is_never_shed_sooner_than_in_its_old_class():
    from sizeup.stt import CONTEXT_FALLBACK, CONTEXT_FALLBACK_MAX_WAIT_S, HIGH, LIVE, SttQueue
    clock = Clock(5_000.0)
    q = SttQueue(clock=clock)
    q.put((LIVE, None, _clip("live", 5_000.0), None, NEXT_DOOR))          # LIVE: never shed
    clock.t += 100
    assert q.raise_channel(NEXT_DOOR, HIGH, 1_000.0) == 1
    assert _take(q, clock.t + 3_000) == ("live", [])                       # well past HIGH's 300 s: still taken
    clock.t = 5_000.0
    q2 = SttQueue(clock=clock)
    q2.put((CONTEXT_FALLBACK, None, _clip("fb", 5_000.0), None, NEXT_DOOR))
    clock.t += 100
    assert q2.raise_channel(NEXT_DOOR, HIGH, 1_000.0) == 1
    assert _take(q2, 5_000.0 + CONTEXT_FALLBACK_MAX_WAIT_S - 10) == ("fb", [])    # its 30 min kept, not HIGH's 5
    q3 = SttQueue(clock=Clock(5_000.0))
    q3.put((CONTEXT_FALLBACK, None, _clip("fb2", 5_000.0), None, NEXT_DOOR))
    q3.clock.t += 100
    q3.raise_channel(NEXT_DOOR, HIGH, 1_000.0)
    assert _take(q3, 5_000.0 + CONTEXT_FALLBACK_MAX_WAIT_S + 1) == (None, ["fb2"])
