"""The local model behind the police rules (sizeup/intel/backing.py): only rows the rules raised nothing with, only the
police pack's scope, capped, never in the way of the rules and never escalating their events; an answer is checked
again here and maps to a new event like a rule's (marked "·m"), or to evidence on an open one; no answer is a no-op;
Bedrock a stub, off. A fake backend only (no model, no network); invented text only."""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from concurrent.futures import Future

import pytest

from sizeup.intel import backing as B
from sizeup.intel import infer as I
from sizeup.intel import rules as rules_mod
from sizeup.intel.engine import IntelEngine, RecordingSink
from sizeup.intel.events import MemoryEvents
from sizeup.intel.push import compose
from tests.test_intel import Clock, example_locate, row

MACHETE = "Caller reports a man waving a machete outside the deli at 100 Example Ave, he is yelling at people walking by"
FIGHT = "Two males fighting in the parking lot at Sample St and Test Rd, one of them is bleeding from the head"
QUIET = "units be advised the parade route closes at noon tomorrow along the avenue"
REASON = "Man with a blade threatening passers-by"
SHOTS = "shots fired at 100 Example Ave"


def police(strip=("weapons", "edp", "dispute", "assault"), settings=None):
    """The police pack without the rules of these categories, so the invented rows below stay rows the rules raise
    nothing with whatever rules the pack gains."""
    raw = json.loads((rules_mod.DATA / "police.json").read_text())
    raw["rules"] = [r for r in raw["rules"] if r["category"] not in strip]
    return rules_mod.load_packs([raw], settings or {})


def full(settings=None):
    return rules_mod.load_packs(None, settings or {})


def answer(category="weapons", confidence=0.85, **kw) -> dict:
    return {"category": category, "confidence": confidence, "in_progress": True, "weapon": "other",
            "deescalation": False, "address": None, "reason": REASON, **kw}


def setup(backend=None, cfg=None, packs=None, clock=None, logs=None, locate=example_locate, settings=None):
    clock = clock or Clock(1050)
    sink = RecordingSink()
    log = (lambda m: logs.append(m)) if logs is not None else (lambda m: None)
    e = IntelEngine(settings={"intel": dict(cfg or {}), **(settings or {})}, events=MemoryEvents(), sink=sink,
                    locate=locate, thread=False, clock=clock, packs=police() if packs is None else packs, log=log)
    b = backend if backend is not None else B.FakeBackend(answer())
    e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=clock, log=log, backend=b)
    return e, sink, b


def fight_cat(e) -> str:
    return "assault" if "assault" in e.pack("police").categories else "dispute"


# -- new events --------------------------------------------------------------------------------------------------------

def test_the_fixture_rules_raise_nothing_for_the_invented_rows():
    for text in (MACHETE, FIGHT):
        sink = RecordingSink()
        e = IntelEngine(settings={}, events=MemoryEvents(), sink=sink, locate=example_locate, thread=False,
                        clock=Clock(1050), packs=police())
        assert e.feed(row("t1", text, 1000)) == [] and sink.pushes == []


def test_a_row_the_rules_left_is_the_models_event_pushed_with_its_marker():
    e, sink, b = setup()
    [ev] = e.feed(row("t1", MACHETE, 1000))
    assert len(b.asked) == 1
    assert (ev.category, ev.status, ev.mode) == ("weapons", "open", "push")
    assert ev.label_full == "100 Example Ave, Port Chester" and ev.confirmed      # the transcript's place, the locator's
    assert ev.traffic_ids == ["t1"] and any(r[0] == "model.fake" and r[2] == 0.85 for r in ev.rules)
    [(_pev, d)] = sink.pushes
    assert d.would_push and d.message["title"].endswith(" ·m")
    shown = e.backing.shown(ev.id)
    assert shown["model"] == "fake-1" and shown["model_version"] == "0000" and shown["confidence"] == 0.85
    assert shown["reason"] == REASON and shown["used"] == "event"
    assert e.backing.today["events"] == 1 and e.backing.today["ok"] == 1 and e.backing.today["asked"] == 1


def test_a_fight_the_rules_left_is_the_models_event():
    e0, _s, _b = setup()
    cat = fight_cat(e0)
    e, sink, b = setup(B.FakeBackend(answer(cat, 0.8, weapon="none")))
    [ev] = e.feed(row("t1", FIGHT, 1000))
    assert ev.category == cat and ev.label_full == "Sample St & Test Rd, Rye"
    assert sink.pushes[0][1].message["title"].endswith(" ·m")


def test_under_the_threshold_or_none_is_nothing():
    for a in (answer(confidence=0.69), answer("none", 0.95)):
        e, sink, b = setup(B.FakeBackend(a))
        assert e.feed(row("t1", MACHETE, 1000)) == [] and e.events.all() == [] and len(b.asked) == 1
    e, sink, b = setup(B.FakeBackend(answer(confidence=0.6)), cfg={"infer_pd_threshold": 0.5})
    assert len(e.feed(row("t1", MACHETE, 1000))) == 1


def test_the_model_mode_caps_its_events_and_a_category_off_is_nothing():
    e, sink, b = setup(cfg={"infer_pd_mode": "map"})
    [ev] = e.feed(row("t1", MACHETE, 1000))
    assert ev.mode == "map" and not sink.pushes[0][1].would_push
    e, sink, b = setup(packs=police(settings={"intel_packs": {"police": {"categories": {"weapons": {"mode": "off"}}}}}))
    assert e.feed(row("t1", MACHETE, 1000)) == [] and e.events.all() == []


def test_scored_on_its_own_rule_and_the_trigger_only_never_another_rows_evidence():
    def ans(msgs):
        trig = msgs[-1]["content"]
        return answer("mva_injury", 0.5) if "glass everywhere" in trig.split("Same talkgroup")[0] else answer("none", 0.1)
    # (under the model-alone cap, ALONE_MAX: t1's hit added in would show over 0.5)
    e, sink, b = setup(B.FakeBackend(ans), packs=full(), cfg={"infer_pd_threshold": 0.4})
    assert e.feed(row("t1", "report of an MVA on Sample St near the gas station", 1000)) == []    # under its threshold
    [ev] = e.feed(row("t2", "a passerby says the driver is not moving and there is glass everywhere", 1020))
    assert ev.category == "mva_injury" and ev.traffic_ids == ["t2"]
    assert [r[0] for r in ev.rules] == ["model.fake"] and ev.what_score == 0.5


# -- the model's confidence alone never pushes -------------------------------------------------------------------------

def test_an_uncorroborated_model_event_is_on_the_card_not_pushed_unless_the_setting_is_off():
    text = "a man waving a machete is shouting at the cars going by on the bridge"      # no cue, no rule word, no place
    e, sink, b = setup()
    [ev] = e.feed(row("t1", text, 1000))
    assert ev.mode == "map" and not sink.pushes[-1][1].would_push
    e, sink, b = setup(cfg={"infer_pd_corroborate": False})
    [ev] = e.feed(row("t1", text, 1000))
    assert ev.mode == "push" and sink.pushes[-1][1].would_push


def test_a_dispatch_cue_a_rule_word_or_a_confirmed_place_corroborates():
    for text in ("respond to the bridge for a man waving a machete at the cars going by",          # a dispatch cue
                 "a man waving a machete outside the deli at 100 Example Ave right now"):          # a confirmed place
        e, sink, b = setup()
        [ev] = e.feed(row("t1", text, 1000))
        assert ev.mode == "push" and sink.pushes[-1][1].would_push, text
    # a rule word of the category under its threshold (the MVA rule alone)
    e, sink, b = setup(B.FakeBackend(answer("mva_injury", 0.9)), packs=full())
    [ev] = e.feed(row("t1", "there was an MVA on the bridge and the driver is not moving at all", 1000))
    assert ev.mode == "push"
    # the model's own address never does (unconfirmed)
    e, sink, b = setup(B.FakeBackend(answer(address="100 Example Ave")),
                       locate=lambda t, s: example_locate(t + " maybe", s))
    [ev] = e.feed(row("t1", "a man with a blade is standing near 100 Example Ave shouting at the cars going by", 1000))
    assert ev.mode == "map"


@pytest.mark.parametrize("said", ["none", "Unknown", "N/A", "", "  "])
def test_an_address_of_none_unknown_or_n_a_is_no_address(said):
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1, address=said)))
    e.feed(row("t1", MACHETE, 1000))
    assert e.backing.records[-1]["why"] == "ok" and e.backing.records[-1]["address"] == ""
    assert B.validate(answer(address=said), ["weapons"])["address"] is None


def test_invalid_answers_show_in_the_health_line():
    e, sink, b = setup(B.FakeBackend(answer(confidence=1.7)))
    e.feed(row("t1", MACHETE, 1000))
    assert "1 invalid" in e.backing.health_row()["text"]


# -- never escalating the rules' events --------------------------------------------------------------------------------

def test_an_answer_landing_in_an_open_rules_event_is_evidence_only():
    e, sink, b = setup(B.FakeBackend(answer("weapons", 0.95)), packs=full())
    [ev] = e.feed(row("t1", SHOTS, 1000))
    before = (ev.level, ev.mode, ev.what_score, list(ev.rules), list(ev.traffic_ids), ev.note)
    pushes = len(sink.pushes)
    assert e.feed(row("t2", "caller says a second person is filming it from across the street", 1030)) == []
    after = e.events.get(ev.id)
    assert (after.level, after.mode, after.what_score, after.rules, after.traffic_ids, after.note) == before
    assert len(sink.pushes) == pushes and len(e.events.all()) == 1
    assert not any(r[0].startswith("model.") for r in after.rules)
    assert e.backing.today["evidence"] == 1 and e.backing.shown(ev.id)["used"] == "evidence"
    assert "·m" not in compose(after, "PD", {"label": "Weapon / shots"})["title"]                  # no marker


def test_another_category_on_the_same_source_in_the_window_is_no_second_pushed_event_nor_its_record():
    e0, _s, _b = setup()
    e, sink, b = setup(B.FakeBackend(answer(fight_cat(e0), 0.9)), packs=full())
    [ev] = e.feed(row("t1", SHOTS, 1000))
    pushes = len(sink.pushes)
    assert e.feed(row("t2", "there are several people in the street pushing each other near the corner", 1040)) == []
    assert len(e.events.all()) == 1 and len(sink.pushes) == pushes
    assert e.backing.shown(ev.id) is None                                   # never recorded against that event
    assert (e.backing.records[-1]["used"], e.backing.records[-1]["event_id"]) == ("near", None)


def test_a_new_call_while_another_is_open_on_the_source_is_its_own_event_on_the_card_not_pushed():
    e0, _s, _b = setup()
    cat = fight_cat(e0)
    e, sink, b = setup(B.FakeBackend(answer(cat, 0.9)), packs=full())
    [shots] = e.feed(row("t1", SHOTS, 1000))
    pushes = len(sink.pushes)
    [ev] = e.feed(row("t2", "new call, two males fighting in the parking lot, one of them is bleeding", 1090))
    assert ev.id != shots.id and ev.category == cat and ev.mode == "map"
    assert len(sink.pushes) == pushes + 1 and not sink.pushes[-1][1].would_push
    assert e.backing.shown(ev.id)["used"] == "event" and e.backing.shown(shots.id) is None


def test_another_place_while_a_long_incident_is_open_is_its_own_event_on_the_card():
    e0, _s, _b = setup()
    e, sink, b = setup(B.FakeBackend(answer(fight_cat(e0), 0.9)), packs=full())
    [shots] = e.feed(row("t1", SHOTS, 1000))
    for i in range(3):                                        # the incident goes on: its event's last_at moves
        e.feed(row(f"u{i}", "more shots fired at 100 Example Ave, units staging", 1010 + 20 * i))
    [ev] = e.feed(row("t2", "neighbor says two men are fighting at Sample St and Test Rd, one is bleeding", 1080))
    assert ev.id != shots.id and ev.label_full == "Sample St & Test Rd, Rye" and ev.mode == "map"
    assert e.events.get(shots.id).traffic_ids[-1] != "t2"


def test_the_models_place_is_set_on_the_row_only_for_an_event_it_raises():
    def exact(t, s):                                          # a place only for the address alone
        return example_locate(t, s) if t.strip().lower() in ("100 example ave", "sample st and test rd") else None
    e, sink, b = setup(B.FakeBackend(answer("weapons", 0.9, address="100 Example Ave")), packs=full(), locate=exact)
    [ev] = e.feed(row("t1", SHOTS, 1000))
    e.feed(row("t2", "a caller near 100 Example Ave says the man is still pacing up and down the block", 1030))
    assert e.backing.records[-1]["used"] == "evidence"
    assert e.windows[("police", "p25:mrrs:9211")].entries["t2"].place is None


def test_out_of_the_window_on_the_source_another_category_is_its_own_event():
    clock = Clock(1050)
    e0, _s, _b = setup()
    e, sink, b = setup(B.FakeBackend(answer(fight_cat(e0), 0.9)), packs=full(), clock=clock)
    e.feed(row("t1", SHOTS, 1000))
    clock.t = 1400
    [ev] = e.feed(row("t2", "there are several people in the street pushing each other near the corner", 1390))
    assert ev.category == fight_cat(e0) and len(e.events.all()) == 2


def test_de_escalation_is_stored_only_and_never_a_note():
    e, sink, b = setup(B.FakeBackend(answer("weapons", 0.9, deescalation=True)), packs=full())
    [ev] = e.feed(row("t1", SHOTS, 1000))
    ev.note = "possibly cancelled"
    e.events.save(ev)
    pushes = len(sink.pushes)
    e.feed(row("t2", "the subject is in custody and the scene is secure, units can slow it down", 1030))
    assert e.events.get(ev.id).note == "possibly cancelled" and len(sink.pushes) == pushes
    assert e.backing.records[-1]["deescalation"] == 1


# -- no answer is a no-op ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("why", ["no_worker", "gaming", "deadline", "invalid"])
def test_no_answer_is_nothing_and_counted(why):
    e, sink, b = setup(B.FakeBackend(answer(), why=why))
    assert e.feed(row("t1", MACHETE, 1000)) == [] and e.events.all() == [] and sink.pushes == []
    assert e.backing.today[why] == 1 and e.backing.today["asked"] == 1


@pytest.mark.parametrize("bad", [
    {**answer(), "extra": 1},                                         # additionalProperties: false
    {k: v for k, v in answer().items() if k != "reason"},             # a key missing
    answer(confidence=1.5), answer(confidence=True), answer(category="arson"), answer(weapon="sword"),
    answer(in_progress="yes"), answer(address="x" * 81), answer(reason="y" * 121), ["weapons"], "weapons",
])
def test_an_invalid_answer_is_nothing(bad):
    e, sink, b = setup(B.FakeBackend(bad))
    assert e.feed(row("t1", MACHETE, 1000)) == [] and e.events.all() == []
    assert e.backing.today["invalid"] == 1


def test_a_backend_that_never_answers_times_out_and_a_late_answer_is_ignored():
    clock = Clock(1050)
    e, sink, b = setup(B.FakeBackend(answer(), hold=True), clock=clock)
    assert e.feed(row("t1", MACHETE, 1000)) == [] and len(b.open) == 1
    clock.t += B.DEADLINE_S + B.GRACE_S + 1
    e.tick(clock.t)
    assert e.backing.today["deadline"] == 1
    b.open[0].set_result((answer(), B.meta("ok", "fake-1")))
    assert e.events.all() == [] and sink.pushes == []


def test_an_answer_arriving_in_time_on_another_thread_maps():
    e, sink, b = setup(B.FakeBackend(answer(), hold=True))
    e.feed(row("t1", MACHETE, 1000))
    t = threading.Thread(target=lambda: b.open[0].set_result((answer(), B.meta("ok", "fake-1"))))
    t.start()
    t.join(5)
    assert [x.category for x in e.events.all()] == ["weapons"]


def test_a_disregard_before_the_answer_spends_it_and_detection_off_is_nothing():
    e, sink, b = setup(B.FakeBackend(answer(), hold=True))
    e.feed(row("t1", MACHETE, 1000))
    e.feed(row("t2", "disregard that last call", 1005))
    b.open[0].set_result((answer(), B.meta("ok")))
    assert e.events.all() == []
    e, sink, b = setup(B.FakeBackend(answer(), hold=True))
    e.feed(row("t1", MACHETE, 1000))
    e.policy.cfg["enabled"] = False
    b.open[0].set_result((answer(), B.meta("ok")))
    assert e.events.all() == []


# -- the gate ----------------------------------------------------------------------------------------------------------

def test_rows_the_rules_raised_are_not_sent_nor_short_suppressed_or_routine_ones():
    e, sink, b = setup(packs=full())
    e.feed(row("t1", SHOTS + ", multiple callers", 1000))                           # the rules' event
    e.feed(row("t2", "copy that car 3", 1010))                                      # under six words
    e.feed(row("t3", "disregard the last call for the units on scene now", 1020))   # suppressed
    e.feed(row("t4", "car 3 will be out on a traffic stop on the avenue", 1030))   # routine
    assert b.asked == []
    e.feed(row("t5", "caller says somebody keeps ringing every doorbell in the building", 1040))
    assert len(b.asked) == 1


def test_a_re_feed_of_a_row_already_in_an_open_event_is_not_sent():
    e, sink, b = setup(packs=full())
    [ev] = e.feed(row("t1", SHOTS, 1000))
    e.feed(row("t1", "there was a loud noise near 100 Example Ave a few minutes ago tonight", 1000))   # its text again
    e.feed(row("t1", "there was a loud noise near 100 Example Ave a few minutes ago tonight", 1000,
               alt_text="a loud noise near the avenue"))                                           # its alt text
    assert b.asked == [] and "t1" in e.events.get(ev.id).traffic_ids


def test_a_deferred_row_or_a_category_whose_mode_is_off_is_the_rules_call():
    e, sink, b = setup(packs=full())
    e.feed(row("t1", "smoke coming from the roof at 100 Example Ave", 1000))         # fire_report
    e.feed(row("t2", "caller says she can smell something burning at 100 Example Ave, FD notified", 1010))   # fire_call
    assert [x.category for x in e.events.all()] == ["fire_report"] and b.asked == []
    e, sink, b = setup(packs=full({"intel_packs": {"police": {"categories": {"weapons": {"mode": "off"}}}}}))
    assert e.feed(row("t1", SHOTS + " in the alley behind the store", 1000)) == []
    assert b.asked == []


def test_only_the_police_packs_scope_is_sent():
    e, sink, b = setup(packs=full())
    left = "Caller reports a man yelling at people walking by outside the deli at 100 Example Ave"   # no rule takes it
    e.feed(row("o1", left, 1000, source="p25:mrrs:9220"))                         # another PD's talkgroup
    e.feed(row("o2", left, 1000, source="p25:mrrs:9211", service="fire"))        # not police
    assert b.asked == []
    e.feed(row("p1", left, 1000, source="p25:mrrs:9213"))
    assert len(b.asked) == 1
    src = (rules_mod.DATA.parents[1] / "sizeup" / "intel" / "backing.py").read_text()
    assert "9211" not in src and "9212" not in src and "9213" not in src          # read from the pack's scope


def test_a_transmission_is_asked_once():
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1)))
    e.feed(row("t1", MACHETE, 1000))
    e.feed(row("t1", MACHETE + " again", 1000))                # a re-transcription
    assert len(b.asked) == 1


def test_the_hour_and_day_caps():
    clock = Clock(1050)
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1)), cfg={"infer_pd_hour_cap": 2}, clock=clock)
    for i in range(3):
        e.feed(row(f"t{i}", f"{QUIET} number {i}", 1000 + i))
    assert len(b.asked) == 2 and e.backing.today["capped"] == 1
    clock.t += 3601
    e.feed(row("t9", QUIET + " later", clock.t - 5))
    assert len(b.asked) == 3
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1)), cfg={"infer_pd_day_cap": 1, "infer_pd_hour_cap": 100})
    for i in range(3):
        e.feed(row(f"t{i}", f"{QUIET} number {i}", 1000 + i))
    assert len(b.asked) == 1 and e.backing.today["capped"] == 2


def test_off_or_no_worker_is_rules_only():
    e, sink, b = setup(cfg={"infer_pd": False})
    e.feed(row("t1", MACHETE, 1000))
    assert b.asked == [] and e.events.all() == []
    e, sink, b = setup(B.FakeBackend(answer(), up=False))
    e.feed(row("t1", MACHETE, 1000))
    assert b.asked == [] and e.backing.today["unavailable"] == 1
    assert "rules only" in e.backing.health_row()["text"] and e.backing.health_row()["level"] == "off"


def test_one_model_per_row_the_old_stage_is_not_asked_about_the_pack():
    class Provider:
        name = "fake"

        def __init__(self):
            self.asked = []

        def run(self, req):
            self.asked.append(req)
            return I.Result("none", 0.1)
    for on, expect in ((True, 0), (False, 1)):
        e, sink, b = setup(B.FakeBackend(answer("none", 0.1)), cfg={"infer": True, "infer_pd": on}, packs=full())
        p = Provider()
        e.infer = I.Inference(lambda: e.policy.cfg, clock=e.clock, log=lambda m: None, provider=p)
        e.feed(row("t1", "report of an MVA on Sample St near the gas station", 1000))      # uncertain for the rules
        assert len(p.asked) == expect


# -- what is sent ------------------------------------------------------------------------------------------------------

def test_the_prompt_the_trigger_its_context_and_the_site_history():
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1)))
    e.backing.history = lambda place, at: ["2026-01-02 EMS / sick · transported", "2025-12-01 FIRE / alarm"]
    e.feed(row("c0", "car 9 back in service from the earlier call on the avenue", 800))       # over two minutes before
    for i in range(6):
        e.feed(row(f"c{i + 1}", f"unit {i} going to be out at the station for a few minutes", 930 + i))
    e.feed(row("x1", "engine responding to an alarm at the school on the other side", 990, source="p25:mrrs:9212"))
    e.feed(row("t1", MACHETE + ", radio 1234567, plate ABC1234", 1000))
    [sys_m, user] = b.asked[-1]
    assert sys_m["role"] == "system" and user["role"] == "user"
    u = user["content"]
    assert "Trigger transmission: " + MACHETE in u and "[id]" in u and "1234567" not in u
    assert "ABC1234" not in u and "[plate]" in u                                              # infer.clean_text
    assert u.count("] unit ") == 5 and "earlier call" not in u and "school" not in u          # 5, this source, 2 min
    assert "2026-01-02 EMS / sick · transported" in u and "Categories" not in u
    # every category with its one-line definition, from the pack's data, in the system prompt
    pack = e.pack("police")
    for name, cd in pack.categories.items():
        assert cd["describe"] and f"- {name}: {cd['label']}. {cd['describe']}" in sys_m["content"]
    assert "- none: " in sys_m["content"]


def test_with_a_wait_the_transmissions_after_it_are_sent_too():
    clock = Clock(1000)
    e, sink, b = setup(B.FakeBackend(answer()), cfg={"infer_pd_after_s": 20}, clock=clock)
    e.feed(row("t1", MACHETE, 1000))
    assert b.asked == []
    clock.t = 1010
    e.feed(row("t2", "he is now walking north toward the bank still holding it", 1010))
    clock.t = 1025
    e.tick(clock.t)
    [msgs] = b.asked
    assert "[+10 s] he is now walking north" in msgs[1]["content"]
    assert [x.category for x in e.events.all()] == ["weapons"]


def test_a_large_context_is_trimmed_to_the_hubs_limits_oldest_first_and_the_trigger_kept_whole():
    trig = "caller states " + "a crowd is gathering by the fountain and getting loud " * 260      # ~14,000 characters
    ctx = [{"at": 900 + i, "text": f"older {i} " + "word " * 2000} for i in range(40)]
    hist = ["2026-01-02 EMS / sick · transported"] * 3
    msgs = B.messages({"weapons": "Weapon / shots"}, {"at": 1000, "text": trig}, ctx, hist)
    assert len(msgs) <= B.MESSAGES_MAX == 16 and sum(len(m["content"]) for m in msgs) <= B.PROMPT_MAX == 32000
    u = msgs[-1]["content"]
    assert "Trigger transmission: " + trig.strip() in u
    kept = [i for i in range(40) if f"] older {i} " in u]
    assert kept and kept == list(range(40 - len(kept), 40))             # the newest kept, the oldest dropped
    m2 = B.messages({"weapons": "W"}, {"at": 0, "text": "x" * 40000}, ctx, hist)
    assert sum(len(m["content"]) for m in m2) <= B.PROMPT_MAX and "older" not in m2[-1]["content"]


def test_the_models_address_counts_only_as_said_in_the_trigger_and_is_never_confirmed():
    loose = lambda t, s: example_locate(t + " maybe", s)       # noqa: E731  (the stand-in's unconfirmed reading)
    text = "a man with a blade is standing near 100 Example Ave shouting at the cars going by"
    e, sink, b = setup(B.FakeBackend(answer(address="100 Example Ave")), locate=loose)
    [ev] = e.feed(row("t1", text, 1000))
    assert ev.label_full == "100 Example Ave, Port Chester" and not ev.confirmed
    # the same address said only in the context, never in the trigger: no place
    def only_the_blade(msgs):
        trig = msgs[-1]["content"].split("Same talkgroup")[0]
        return answer(address="100 Example Ave") if "blade" in trig else answer("none", 0.1)
    e, sink, b = setup(B.FakeBackend(only_the_blade))
    e.feed(row("c1", "car 4 is clear of 100 Example Ave and back in service now", 990))
    [ev] = e.feed(row("t1", "a man with a blade is standing on the corner shouting at the cars going by", 1000))
    assert ev.precision == "none"


def test_the_schema_follows_the_packs_categories():
    s = B.schema(["weapons", "assault"])
    assert s["additionalProperties"] is False and s["required"] == list(B.KEYS)
    assert s["properties"]["category"]["enum"] == ["weapons", "assault", "none"]
    assert s["properties"]["weapon"]["enum"] == ["none", "knife", "gun", "other", "unknown"]
    raw = json.loads((rules_mod.DATA / "police.json").read_text())
    raw["categories"]["assault"] = {"label": "Assault", "mode": "push", "priority": 4, "ttl_min": 45}
    raw["rules"] = [r for r in raw["rules"] if r["category"] not in ("dispute", "assault")]
    e, sink, b = setup(B.FakeBackend(answer("assault", 0.9)), packs=rules_mod.load_packs([raw]))
    [ev] = e.feed(row("t1", FIGHT, 1000))
    assert ev.category == "assault" and "- assault: Assault" in b.asked[0][0]["content"]


def test_no_prompt_or_answer_in_the_logs():
    logs: list[str] = []
    for bk in (B.FakeBackend(answer()), B.FakeBackend(answer(), why="gaming"), B.FakeBackend({"bad": 1})):
        e, sink, b = setup(bk, logs=logs)
        e.feed(row("t1", MACHETE, 1000))
        e.tick(2000)
    e, sink, b = setup(logs=logs, cfg={"infer_pd_bedrock": True, "infer_pd_bedrock_budget_usd": 5})
    e.backing._fixed = None
    e.feed(row("t1", MACHETE, 1000))
    text = "\n".join(logs).lower()
    for w in ("machete", "deli", "yelling", REASON.lower(), "example ave"):
        assert w not in text


# -- the backends ------------------------------------------------------------------------------------------------------

def test_bedrock_is_off_by_default_and_a_stub():
    assert I.DEFAULTS["infer_pd_bedrock"] is False and I.DEFAULTS["infer_pd_bedrock_budget_usd"] == 0
    assert I.DEFAULTS["infer_pd"] is True and I.DEFAULTS["infer_pd_threshold"] == 0.7
    bd = B.BedrockBackend()
    assert not bd.available()
    with pytest.raises(NotImplementedError):
        bd.classify([], {})
    m = B.ModelBacking(lambda: {"infer_pd": False, "infer_pd_bedrock": True, "infer_pd_bedrock_budget_usd": 50})
    assert m.backend() is m.null                       # on, with a budget: still never used (not built)
    assert m.status()["bedrock"] == {"on": True, "budget_usd": 50, "state": "not built"}
    js = (rules_mod.DATA.parents[1] / "sizeup" / "web" / "static" / "intel.js").read_text()
    assert "infer_pd_bedrock" not in js                # no control for it until it is built


def test_the_pc_worker_through_the_hubs_api(monkeypatch):
    assert B.PCWorkerBackend().available() in (True, False)            # the module may not be built here: never raises
    calls = {}

    def submit(task, messages, schema, deadline_s=20, priority=0, meta=False):
        calls.update(task=task, deadline_s=deadline_s, meta=meta, priority=priority, schema=schema, messages=messages)
        return B.resolved((answer(), B.meta("ok", "local-8b", model_version="ab12cd", worker="w1", latency_ms=900)))
    fake = types.SimpleNamespace(submit=submit, available=lambda: True)
    monkeypatch.setitem(sys.modules, "sizeup.cloud.infer", fake)
    import sizeup.cloud
    monkeypatch.setattr(sizeup.cloud, "infer", fake, raising=False)
    clock = Clock(1050)
    sink = RecordingSink()
    e = IntelEngine(settings={}, events=MemoryEvents(), sink=sink, locate=example_locate, thread=False, clock=clock,
                    packs=police())
    e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=clock, log=lambda m: None)
    assert e.backing.backend().name == "pc"
    [ev] = e.feed(row("t1", MACHETE, 1000))
    assert calls["task"] == "pd_classify" and calls["deadline_s"] == 15 and calls["meta"] is True
    assert calls["schema"]["additionalProperties"] is False and len(calls["messages"]) == 2
    assert any(r[0] == "model.pc" for r in ev.rules)
    st = e.backing.status()
    assert st["model"] == "local-8b" and st["model_version"] == "ab12cd" and st["backend"] == "pc"
    assert e.backing.health_row()["text"].startswith("model: PC worker ok")
    assert e.backing.shown(ev.id)["model_version"] == "ab12cd"


def test_a_backend_future_of_a_bare_dict_still_maps():
    class Bare(B.FakeBackend):
        def classify(self, messages, schema, deadline_s=B.DEADLINE_S):
            f: Future = Future()
            f.set_result(answer())
            return f
    e, sink, b = setup(Bare())
    assert len(e.feed(row("t1", MACHETE, 1000))) == 1


def test_a_backend_that_waits_never_holds_the_engine():
    class Slow(B.FakeBackend):
        def available(self):
            time.sleep(2)
            return True

        def classify(self, messages, schema, deadline_s=B.DEADLINE_S):
            time.sleep(2)
            return super().classify(messages, schema, deadline_s)
    clock = Clock(1050)
    e = IntelEngine(settings={}, events=MemoryEvents(), sink=RecordingSink(), locate=example_locate, thread=False,
                    clock=clock, packs=police())
    e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=clock, log=lambda m: None, backend=Slow(answer()),
                               worker=True)
    t0 = time.monotonic()
    assert e.feed(row("t1", MACHETE, 1000)) == []                         # queued, never waited for
    t1 = time.monotonic()
    e.backing.status()
    e.backing.health_row()
    t2 = time.monotonic()
    assert t1 - t0 < 0.5 and t2 - t1 < 0.5, (t1 - t0, t2 - t1)
    time.sleep(0.2)
    assert e._lock.acquire(timeout=0.5)                                    # the model thread waits without it
    e._lock.release()
    end = time.time() + 15
    while not e.events.all() and time.time() < end:
        time.sleep(0.05)
    assert [x.category for x in e.events.all()] == ["weapons"]


def test_live_even_an_answer_already_resolved_is_applied_on_the_model_thread():
    clock = Clock(1050)
    e = IntelEngine(settings={}, events=MemoryEvents(), sink=RecordingSink(), locate=example_locate, thread=False,
                    clock=clock, packs=police())
    e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=clock, log=lambda m: None, backend=B.FakeBackend(answer()),
                               worker=True)
    seen = []
    orig = e.backing._answered
    e.backing._answered = lambda *a: (seen.append(threading.current_thread().name), orig(*a))[1]
    e.feed(row("t1", MACHETE, 1000))
    end = time.time() + 5
    while not e.events.all() and time.time() < end:
        time.sleep(0.01)
    assert seen == ["intel-model"] and [x.category for x in e.events.all()] == ["weapons"]


def test_live_the_answer_is_applied_on_the_models_own_thread():
    clock = Clock(1050)
    e = IntelEngine(settings={}, events=MemoryEvents(), sink=RecordingSink(), locate=example_locate, thread=False,
                    clock=clock, packs=police())
    b = B.FakeBackend(answer(), hold=True)
    seen = []
    e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=clock, log=lambda m: None, backend=b, worker=True)
    orig = e.backing._answered
    e.backing._answered = lambda *a: (seen.append(threading.current_thread().name), orig(*a))[1]
    e.feed(row("t1", MACHETE, 1000))
    end = time.time() + 5
    while not b.open and time.time() < end:                   # asked on the model thread, a moment later
        time.sleep(0.01)
    t = threading.Thread(target=lambda: b.open[0].set_result((answer(), B.meta("ok"))), name="hub-resolver")
    t.start()
    t.join(5)
    end = time.time() + 5
    while not e.events.all() and time.time() < end:
        time.sleep(0.01)
    assert seen == ["intel-model"] and [x.category for x in e.events.all()] == ["weapons"]


# -- the push marker, Settings, the record ---------------------------------------------------------------------------

def test_compose_marks_the_models_events_only():
    from sizeup.intel.model import Event
    ev = Event(id=1, category="weapons", rules=[["model.pc", 1, 0.8]], first_at=1000)
    assert compose(ev, "PD", {"label": "Weapon / shots"})["title"] == "PD · Weapon / shots ·m"
    ev.rules = [["pd.shots", 1, 0.75]]
    assert compose(ev, "PD", {"label": "Weapon / shots"})["title"] == "PD · Weapon / shots"


def test_settings_take_the_models_keys(tmp_path):
    from sizeup.cad.api import CadApi
    from tests.test_cad import make_cad
    from tests.test_roles import CASEY, JORDAN, call, people
    cad = make_cad(tmp_path)
    people(cad)
    api = CadApi(cad)
    post = lambda b, who=CASEY: call(api, "/api/cad/settings/intel", b, who)      # noqa: E731
    assert post({"action": "set", "key": "infer_pd_threshold", "value": 0.8}, JORDAN)[0] == 403     # admins only
    assert post({"action": "set", "key": "infer_pd_threshold", "value": 0.8})[0] == 200
    assert post({"action": "set", "key": "infer_pd_hour_cap", "value": 10})[0] == 200
    assert post({"action": "set", "key": "infer_pd_bedrock_budget_usd", "value": 500})[0] == 400
    assert post({"action": "set", "key": "infer_pd_bedrock", "value": "yes"})[0] == 400
    assert post({"action": "set", "key": "infer_pd_mode", "value": "loud"})[0] == 400
    code, out = post({"action": "set", "key": "infer_pd_bedrock", "value": True})
    assert code == 200 and out["intel"]["model"]["bedrock"]["on"] is True
    st = cad.intel.backing.status()
    assert st["cfg"]["infer_pd_threshold"] == 0.8 and st["cfg"]["infer_pd_hour_cap"] == 10
    assert st["backend"] in ("none", "pc")                               # Bedrock on: still never used
    assert cad.store.one("SELECT name FROM sqlite_master WHERE name = 'intel_model'") is None      # never at start


def test_the_record_in_the_store_the_more_info_bundle_and_the_prune(tmp_path):
    from sizeup import context_bundle, sourcekeys
    from tests.test_cad import make_cad
    cad = make_cad(tmp_path)
    clock = Clock(cad.clock())
    cad.intel.backing = B.ModelBacking(lambda: cad.intel.policy.cfg, store=cad.store, clock=clock, log=lambda m: None,
                                       backend=B.FakeBackend(answer()))
    cad.intel.locate = example_locate
    cad.intel.packs = police()
    t = cad.clock()
    [ev] = cad.intel.feed(row("t1", MACHETE, t))
    r = cad.store.one("SELECT * FROM intel_model")
    assert r["model"] == "fake-1" and r["used"] == "event" and r["event_id"] == ev.id
    assert cad.intel.backing.shown(ev.id)["reason"] == REASON
    with sourcekeys.request_context("someone verified"):
        bundle = context_bundle.build(cad, "intel", ev.id, admin=True)
    assert bundle["why"]["model"]["confidence"] == 0.85 and bundle["why"]["model"]["model"] == "fake-1"
    assert context_bundle.site_lines(cad, "", None, None, t) == []          # no site: no history
    assert context_bundle.site_lines(cad, "100 Example Ave, Port Chester", 41.0, -73.66, t) == []   # no calls there
    # another process (a restart) prunes what an earlier one wrote, without writing anything first
    fresh = B.ModelBacking(lambda: {}, store=cad.store, clock=clock, log=lambda m: None)
    fresh.prune(t + B.KEEP_S + 1)
    assert cad.store.one("SELECT COUNT(*) AS n FROM intel_model")["n"] == 0


# -- with the FD already there (engine._fire): the model's fire events are held the same ------------------------------

def test_a_model_fire_event_at_a_place_the_fd_is_working_is_held_like_a_rules_one():
    from tests.test_intel_fd_present import NOW, engine as fd_engine, inc, locate as fd_locate
    text = "Car 3, check on a dumpster behind 20 Example Road, the caller says it is glowing and crackling"
    for incs, pushes in (((inc(fd="dispatched"),), False), ((), True)):
        e, sink = fd_engine(*incs)
        b = B.FakeBackend(answer("fire_report", 0.9, weapon="none"))
        e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=e.clock, log=lambda m: None, backend=b)
        [ev] = e.feed(row("t1", text, NOW))
        assert len(b.asked) == 1 and ev.category == "fire_report" and any(r[0] == "model.fake" for r in ev.rules)
        assert bool([d for _e, d in sink.pushes if d.would_push]) is pushes, incs
        if incs:
            assert ev.suppressed == "fd_dispatched" and ev.near_incident == 41 and ev.fd == "dispatched"
    assert fd_locate(text, {}) is not None


# -- the real sizeup.cloud.infer (#733) --------------------------------------------------------------------------------

def test_the_real_hub_module_with_no_worker_is_no_worker_at_once_counted_never_raising():
    from sizeup.cloud import infer as hub_infer
    if hub_infer._lane is not None:
        hub_infer.uninstall(hub_infer._lane)
    pc = B.PCWorkerBackend()
    assert pc.mod() is hub_infer and pc.available() is False and pc.state() == "unavailable"
    f = pc.classify(B.messages({"weapons": "W"}, {"at": 0, "text": MACHETE}, [], []), B.schema(["weapons"]))
    assert f.done() and f.result()[0] is None and f.result()[1]["why"] == "no_worker"

    class Asking(B.PCWorkerBackend):                 # as if a worker had just gone: the hub says no_worker at once
        def available(self):
            return True
    clock = Clock(1050)
    e = IntelEngine(settings={}, events=MemoryEvents(), sink=RecordingSink(), locate=example_locate, thread=False,
                    clock=clock, packs=police())
    e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=clock, log=lambda m: None, backend=Asking())
    t0 = time.monotonic()
    assert e.feed(row("t1", MACHETE, 1000)) == []
    assert time.monotonic() - t0 < 1.0 and e.events.all() == []
    assert e.backing.today["no_worker"] == 1 and "1 no_worker" in e.backing.health_row()["title"]
    e.backing._fixed = None                          # the real one: no worker, rules only
    assert "rules only" in e.backing.health_row()["text"]


def test_the_real_hub_module_with_a_worker_answers_through_its_lane():
    from sizeup.cloud import infer as hub_infer
    from tests.test_infer_lane import MODELS, lane
    ln = lane()
    ln.seen("gpu-1", "online", MODELS)
    hub_infer.install(ln)
    try:
        assert B.PCWorkerBackend().available() is True
        clock = Clock(1050)
        e = IntelEngine(settings={}, events=MemoryEvents(), sink=RecordingSink(), locate=example_locate, thread=False,
                        clock=clock, packs=police())
        e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=clock, log=lambda m: None)
        assert e.feed(row("t1", MACHETE, 1000)) == []             # asked: the worker hasn't claimed it yet
        job = ln.claim("gpu-1", 0, "online", MODELS)
        pub = ln.public(job, ln.clock())
        assert pub["task"] == "pd_classify" and pub["deadline_s"] == 15.0 and len(pub["messages"]) == 2
        assert ln.complete("gpu-1", job.lease, {"output": answer(), "tokens_in": 700, "tokens_out": 60, "infer_ms": 800})
        end = time.time() + 5
        while not e.events.all() and time.time() < end:
            time.sleep(0.01)
        [ev] = e.events.all()
        assert ev.category == "weapons" and any(r[0] == "model.pc" for r in ev.rules)
        assert e.backing.shown(ev.id)["model"] == MODELS["llm"] and e.backing.shown(ev.id)["model_version"] == MODELS["llm_version"]
    finally:
        hub_infer.uninstall(ln)
        ln._results.shutdown(wait=False)


# -- an unclear transcript, a category's cues, the model alone (a model category raised "high" from one looped
# transcript and a short row of context: invented shapes) --------------------------------------------------------------

LOOPED = "So then we were" + " out of the" * 60      # whisper's decoder loop on a 2 s clip
WATERY = "Boat ramp lot, copy."                       # a short row of another radio that says a cue, nothing more
NO_CUE = "she says he went out past the rocks a while ago and has not come back yet to the house"
CUED = "a kayak flipped over out past the rocks and she cannot see him anymore from where she is"


def water(confidence=0.95, **kw) -> dict:
    return answer("water_rescue", confidence, **{"weapon": "none", "in_progress": False, "reason": "says water", **kw})


def test_a_looped_transcript_is_never_read_nor_asked_and_raises_nothing():
    e, sink, b = setup(B.FakeBackend(water()))
    assert e.feed(row("t1", WATERY, 1000, end=1002.5)) == []
    assert e.feed(row("t2", LOOPED, 1006, end=1008)) == []                  # (the words alone say it: a loop)
    assert e.feed(row("t3", "", 1010, end=1012, status="unclear")) == []    # (as the CAD of record stores it)
    assert b.asked == [] and e.events.all() == [] and sink.pushes == [] and e.unclear == 2


def test_a_category_with_cues_is_raised_only_when_the_trigger_says_one_or_two_context_rows_do():
    e, sink, b = setup(B.FakeBackend(water()))
    e.feed(row("t1", WATERY, 1000, end=1002.5))                              # one row of context says "water"
    assert e.feed(row("t2", NO_CUE, 1006)) == [] and len(b.asked) == 1
    assert e.events.all() == [] and e.backing.today["uncued"] == 1
    e, sink, b = setup(B.FakeBackend(water()))                               # two rows say it: a sequence
    e.feed(row("t1", WATERY, 1000, end=1002.5))
    e.feed(row("t2", "Water up past the dock.", 1003, end=1005))          # (each short: never asked itself)
    [ev] = e.feed(row("t3", NO_CUE, 1010))
    assert ev.category == "water_rescue" and ev.traffic_ids == ["t3"]
    e, sink, b = setup(B.FakeBackend(water()))                               # the trigger says it
    [ev] = e.feed(row("t1", CUED, 1000))
    assert ev.category == "water_rescue"


def test_the_model_alone_is_never_high_nor_pushed_and_one_short_row_is_low():
    e, sink, b = setup(B.FakeBackend(water(0.95)))
    [ev] = e.feed(row("t1", CUED, 1000))                                     # no rule word, no place: the model alone
    assert (ev.what_score, ev.level, ev.mode) == (0.6, "medium", "map") and not sink.pushes[-1][1].would_push
    e, sink, b = setup(B.FakeBackend(water(0.95)))
    [ev] = e.feed(row("t1", "a kayak flipped near the rocks, he went under", 1000, end=1002))   # 2 s, 9 words
    assert (ev.what_score, ev.level) == (0.45, "low")
    e, sink, b = setup(B.FakeBackend(water(0.95)), cfg={"infer_pd_corroborate": False})
    [ev] = e.feed(row("t1", CUED, 1000))                                     # pushes allowed: still never "high"
    assert ev.level == "medium"


def test_a_garbled_answer_is_invalid_never_a_category():
    for bad in ({"category": "water_rescue"}, water(confidence="high"), water(confidence=float("nan")),
                dict(water(), category="Water rescue"), dict(water(), extra=1), "water_rescue", None, []):
        e, sink, b = setup(B.FakeBackend(bad))
        assert e.feed(row("t1", CUED, 1000)) == [] and e.events.all() == [], bad
        assert e.backing.today["invalid"] == 1 and e.backing.today["events"] == 0


def test_an_unclear_row_is_never_the_models_context_either():
    e, sink, b = setup(B.FakeBackend(water()))
    e.feed(row("t1", LOOPED, 1000, end=1002))                                      # a loop
    e.feed(row("t2", "Yo creo que el barco está por aquí pero no sé dónde", 1004))  # not English
    e.feed(row("t3", "Boat ramp lot, copy.", 1008, end=1010))                      # a clean short row: context
    e.feed(row("t4", CUED, 1012))
    assert len(b.asked) == 1                                                     # only the clean trigger was asked
    user = b.asked[0][-1]["content"]
    assert "out of the out of the" not in user and "barco" not in user and "Boat ramp lot" in user
    assert e.unclear == 2
