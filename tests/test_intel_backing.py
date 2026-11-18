"""The local model behind the police rules (sizeup/intel/backing.py): only rows the rules left alone, only the police
pack's scope, capped, never in the way of the rules; an answer is checked again here and maps to an event exactly like
a rule's (marked "·m"); no answer is a no-op; Bedrock a stub, off. A fake backend only (no model, no network);
synthetic text only."""

from __future__ import annotations

import json
import sys
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

# the two shapes the rules once missed (synthetic): a weapon in a domestic call, an assault with the other party gone
KNIFE = "Respond to 100 Example Ave, caller states her teenage son is destroying his room, there is a knife in the house"
ASSAULT = "Car 3 respond to Sample St and Test Rd for an assault, the other party is no longer on scene"
REASON = "Teen with a knife breaking things at home"


def police(strip=("weapons", "edp", "dispute", "assault")):
    """The police pack without the rules of these categories: the rows the rules miss stay missed, whatever rules a
    later pack adds for them."""
    raw = json.loads((rules_mod.DATA / "police.json").read_text())
    raw["rules"] = [r for r in raw["rules"] if r["category"] not in strip]
    return rules_mod.load_packs([raw])


def answer(category="weapons", confidence=0.85, **kw) -> dict:
    return {"category": category, "confidence": confidence, "in_progress": True, "weapon": "knife",
            "deescalation": False, "address": None, "reason": REASON, **kw}


def setup(backend=None, cfg=None, packs=None, clock=None, logs=None, locate=example_locate):
    clock = clock or Clock(1050)
    sink = RecordingSink()
    log = (lambda m: logs.append(m)) if logs is not None else (lambda m: None)
    e = IntelEngine(settings={"intel": dict(cfg or {})}, events=MemoryEvents(), sink=sink, locate=locate, thread=False,
                    clock=clock, packs=police() if packs is None else packs, log=log)
    b = backend if backend is not None else B.FakeBackend(answer())
    e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=clock, log=log, backend=b)
    return e, sink, b


def assault_cat(e) -> str:
    return "assault" if "assault" in e.pack("police").categories else "dispute"


# -- the two shapes -------------------------------------------------------------------------------------------------------

def test_the_rules_alone_miss_both_shapes():
    for text in (KNIFE, ASSAULT):
        sink = RecordingSink()
        e = IntelEngine(settings={}, events=MemoryEvents(), sink=sink, locate=example_locate, thread=False,
                        clock=Clock(1050), packs=police())
        assert e.feed(row("t1", text, 1000)) == [] and sink.pushes == []


def test_a_knife_dispatch_the_rules_miss_is_the_models_event_pushed_with_its_marker():
    e, sink, b = setup()
    out = e.feed(row("t1", KNIFE, 1000))
    assert len(b.asked) == 1
    [ev] = out
    assert (ev.category, ev.status, ev.mode) == ("weapons", "open", "push")
    assert ev.label_full == "100 Example Ave, Port Chester" and ev.confirmed      # the transcript's place, the locator's
    assert any(r[0] == "model.fake" and r[2] == 0.85 for r in ev.rules)
    [(pev, d)] = sink.pushes
    assert d.would_push and d.message["title"].endswith(" ·m")
    shown = e.backing.shown(ev.id)
    assert shown["model"] == "fake-1" and shown["model_version"] == "0000" and shown["confidence"] == 0.85
    assert shown["reason"] == REASON and shown["used"] == "event" and shown["weapon"] == "knife"
    assert e.backing.today["events"] == 1 and e.backing.today["ok"] == 1 and e.backing.today["asked"] == 1


def test_an_assault_with_the_other_party_gone_is_the_models_event():
    e0, _s, _b = setup()
    cat = assault_cat(e0)
    e, sink, b = setup(B.FakeBackend(answer(cat, 0.8, weapon="none", in_progress=False)))
    [ev] = e.feed(row("t1", ASSAULT, 1000))
    assert ev.category == cat and ev.label_full == "Sample St & Test Rd, Rye"
    assert sink.pushes and sink.pushes[0][1].message["title"].endswith(" ·m")


def test_a_rules_event_has_no_marker():
    e, sink, b = setup(packs=rules_mod.load_packs(None, {}))
    e.feed(row("t1", "shots fired at 100 Example Ave", 1000))
    assert sink.pushes and not sink.pushes[0][1].message["title"].endswith("·m")
    assert b.asked == []                                              # the rules pushed it: never asked


def test_the_place_from_the_models_address_only_if_said_and_never_confirmed():
    text = "units be advised, a male with a blade was seen near 100 Example Ave maybe, the caller is hiding"
    e, sink, b = setup(B.FakeBackend(answer(address="100 Example Ave")), locate=lambda t, s: example_locate(t + " maybe", s))
    [ev] = e.feed(row("t1", text, 1000))
    assert ev.label_full == "100 Example Ave, Port Chester" and not ev.confirmed
    e2, _s, _b = setup(B.FakeBackend(answer(address="200 Nowhere Blvd")), locate=lambda t, s: None)
    [ev2] = e2.feed(row("t1", "a male with a blade was seen running, the caller is hiding in the store", 1000))
    assert ev2.precision == "none"                                    # words never said: no place


def test_under_the_threshold_or_none_is_nothing():
    for a in (answer(confidence=0.69), answer("none", 0.95)):
        e, sink, b = setup(B.FakeBackend(a))
        assert e.feed(row("t1", KNIFE, 1000)) == [] and e.events.all() == [] and len(b.asked) == 1
    e, sink, b = setup(B.FakeBackend(answer(confidence=0.6)), cfg={"infer_pd_threshold": 0.5})
    assert len(e.feed(row("t1", KNIFE, 1000))) == 1


def test_the_model_mode_caps_its_events():
    e, sink, b = setup(cfg={"infer_pd_mode": "map"})
    [ev] = e.feed(row("t1", KNIFE, 1000))
    assert ev.mode == "map" and sink.pushes and not sink.pushes[0][1].would_push


# -- no answer is a no-op -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("why", ["no_worker", "gaming", "deadline", "invalid"])
def test_no_answer_is_nothing_and_counted(why):
    e, sink, b = setup(B.FakeBackend(answer(), why=why))
    assert e.feed(row("t1", KNIFE, 1000)) == [] and e.events.all() == [] and sink.pushes == []
    assert e.backing.today[why] == 1 and e.backing.today["asked"] == 1


@pytest.mark.parametrize("bad", [
    {**answer(), "extra": 1},                                         # additionalProperties: false
    {k: v for k, v in answer().items() if k != "reason"},             # a key missing
    answer(confidence=1.5), answer(confidence=True), answer(category="arson"), answer(weapon="sword"),
    answer(in_progress="yes"), answer(address="x" * 81), answer(reason="y" * 121), ["weapons"], "weapons",
])
def test_an_invalid_answer_is_nothing(bad):
    e, sink, b = setup(B.FakeBackend(bad))
    assert e.feed(row("t1", KNIFE, 1000)) == [] and e.events.all() == []
    assert e.backing.today["invalid"] == 1


def test_a_backend_that_never_answers_times_out_and_a_late_answer_is_ignored():
    clock = Clock(1050)
    e, sink, b = setup(B.FakeBackend(answer(), hold=True), clock=clock)
    assert e.feed(row("t1", KNIFE, 1000)) == [] and len(b.open) == 1
    clock.t += B.DEADLINE_S + B.GRACE_S + 1
    e.tick(clock.t)
    assert e.backing.today["deadline"] == 1
    b.open[0].set_result((answer(), B.meta("ok", "fake-1")))
    assert e.events.all() == [] and sink.pushes == []


def test_an_answer_arriving_in_time_on_another_thread_maps():
    e, sink, b = setup(B.FakeBackend(answer(), hold=True))
    e.feed(row("t1", KNIFE, 1000))
    import threading
    t = threading.Thread(target=lambda: b.open[0].set_result((answer(), B.meta("ok", "fake-1"))))
    t.start()
    t.join(5)
    assert [x.category for x in e.events.all()] == ["weapons"]


def test_a_disregard_before_the_answer_spends_it():
    e, sink, b = setup(B.FakeBackend(answer(), hold=True))
    e.feed(row("t1", KNIFE, 1000))
    e.feed(row("t2", "disregard that last call", 1005))
    b.open[0].set_result((answer(), B.meta("ok")))
    assert e.events.all() == []


# -- the gate ----------------------------------------------------------------------------------------------------------

def test_rows_the_rules_raised_are_not_sent_nor_short_suppressed_or_routine_ones():
    e, sink, b = setup(packs=rules_mod.load_packs(None, {}))
    e.feed(row("t1", "shots fired at 100 Example Ave, multiple callers", 1000))     # the rules' event
    e.feed(row("t2", "copy that car 3", 1010))                                      # under six words
    e.feed(row("t3", "disregard the last call for the units on scene now", 1020))   # suppressed
    e.feed(row("t4", "car 3 will be out on a traffic stop on the avenue", 1030))   # routine
    assert b.asked == []
    e.feed(row("t5", "caller says somebody is banging on every door in the hallway", 1040))
    assert len(b.asked) == 1


def test_only_the_police_packs_scope_is_sent():
    e, sink, b = setup(packs=rules_mod.load_packs(None, {}))
    e.feed(row("o1", KNIFE, 1000, source="p25:mrrs:9220"))     # another PD's talkgroup (neighbor_police reads it)
    e.feed(row("o2", KNIFE, 1000, source="p25:mrrs:9211", service="fire"))     # not police
    assert b.asked == []
    e.feed(row("p1", KNIFE, 1000, source="p25:mrrs:9213"))
    assert len(b.asked) == 1
    scope = e.pack("police").scope["sources"]
    assert all(s.startswith("p25:mrrs:") for s in scope)       # read from the pack, never named in backing.py
    src = (rules_mod.DATA.parents[1] / "sizeup" / "intel" / "backing.py").read_text()
    assert "9211" not in src and "9212" not in src and "9213" not in src


def test_a_transmission_is_asked_once():
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1)))
    e.feed(row("t1", KNIFE, 1000))
    e.feed(row("t1", KNIFE + " again", 1000))                  # a re-transcription
    assert len(b.asked) == 1


def test_the_hour_and_day_caps():
    clock = Clock(1050)
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1)), cfg={"infer_pd_hour_cap": 2}, clock=clock)
    for i in range(3):
        e.feed(row(f"t{i}", f"{KNIFE} number {i}", 1000 + i))
    assert len(b.asked) == 2 and e.backing.today["capped"] == 1
    clock.t += 3601
    e.feed(row("t9", KNIFE + " later", clock.t - 5))
    assert len(b.asked) == 3
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1)), cfg={"infer_pd_day_cap": 1, "infer_pd_hour_cap": 100})
    for i in range(3):
        e.feed(row(f"t{i}", f"{KNIFE} number {i}", 1000 + i))
    assert len(b.asked) == 1 and e.backing.today["capped"] == 2


def test_off_or_no_worker_is_rules_only():
    e, sink, b = setup(cfg={"infer_pd": False})
    e.feed(row("t1", KNIFE, 1000))
    assert b.asked == [] and e.events.all() == []
    e, sink, b = setup(B.FakeBackend(answer(), up=False))
    e.feed(row("t1", KNIFE, 1000))
    assert b.asked == [] and e.backing.today["unavailable"] == 1
    assert "rules only" in e.backing.health_row()["text"] and e.backing.health_row()["level"] == "off"


# -- what is sent ------------------------------------------------------------------------------------------------------

def test_the_prompt_the_trigger_its_context_and_the_site_history():
    e, sink, b = setup(B.FakeBackend(answer("none", 0.1)))
    e.backing.history = lambda place, at: ["2026-01-02 EMS / sick · transported", "2025-12-01 FIRE / alarm"]
    e.feed(row("c0", "car 9 back in service from the earlier call on the avenue", 800))       # over two minutes before
    for i in range(6):
        e.feed(row(f"c{i + 1}", f"unit {i} going to be out at the station for a few minutes", 930 + i))
    e.feed(row("x1", "engine responding to an alarm at the school on the other side", 990, source="p25:mrrs:9212"))
    e.feed(row("t1", KNIFE + ", radio 1234567", 1000))
    [sys_m, user] = b.asked[-1]
    assert sys_m["role"] == "system" and user["role"] == "user"
    u = user["content"]
    assert "Trigger transmission: " + KNIFE in u and "[id]" in u and "1234567" not in u
    assert u.count("] unit ") == 5 and "earlier call" not in u and "school" not in u          # 5, this source, 2 min
    assert "- weapons: Weapon / shots" in u and "2026-01-02 EMS / sick · transported" in u


def test_a_large_context_is_trimmed_to_the_hubs_limits_oldest_first_and_the_trigger_kept_whole():
    trig = "caller states " + "the neighbor is screaming and throwing things " * 300      # ~14,000 characters
    ctx = [{"at": 900 + i, "text": f"older {i} " + "word " * 2000} for i in range(40)]
    hist = ["2026-01-02 EMS / sick · transported"] * 3
    msgs = B.messages({"weapons": "Weapon / shots"}, {"at": 1000, "text": trig}, ctx, hist)
    assert len(msgs) <= B.MESSAGES_MAX == 16 and sum(len(m["content"]) for m in msgs) <= B.PROMPT_MAX == 32000
    u = msgs[-1]["content"]
    assert "Trigger transmission: " + trig.strip() in u
    kept = [i for i in range(40) if f"] older {i} " in u]
    assert kept and kept == list(range(40 - len(kept), 40))             # the newest kept, the oldest dropped
    huge = "x" * 40000
    m2 = B.messages({"weapons": "W"}, {"at": 0, "text": huge}, ctx, hist)
    assert sum(len(m["content"]) for m in m2) <= B.PROMPT_MAX and "older" not in m2[-1]["content"]


def test_with_a_wait_the_transmissions_after_it_are_sent_too():
    clock = Clock(1000)
    e, sink, b = setup(B.FakeBackend(answer()), cfg={"infer_pd_after_s": 20}, clock=clock)
    e.feed(row("t1", KNIFE, 1000))
    assert b.asked == []
    clock.t = 1010
    e.feed(row("t2", "the son has locked himself in the bathroom with it", 1010))
    clock.t = 1025
    e.tick(clock.t)
    [msgs] = b.asked
    assert "[+10 s] the son has locked himself" in msgs[1]["content"]
    assert [x.category for x in e.events.all()] == ["weapons"]


def test_the_schema_follows_the_packs_categories():
    s = B.schema(["weapons", "assault"])
    assert s["additionalProperties"] is False and s["required"] == list(B.KEYS)
    assert s["properties"]["category"]["enum"] == ["weapons", "assault", "none"]
    assert s["properties"]["weapon"]["enum"] == ["none", "knife", "gun", "other", "unknown"]
    raw = json.loads((rules_mod.DATA / "police.json").read_text())
    raw["categories"]["assault"] = {"label": "Assault", "mode": "push", "priority": 4, "ttl_min": 45}
    e, sink, b = setup(B.FakeBackend(answer("assault", 0.9)), packs=rules_mod.load_packs([raw]))
    [ev] = e.feed(row("t1", ASSAULT, 1000))
    assert ev.category == "assault" and "- assault: Assault" in b.asked[0][1]["content"]


def test_no_prompt_or_answer_in_the_logs():
    logs: list[str] = []
    for bk in (B.FakeBackend(answer()), B.FakeBackend(answer(), why="gaming"), B.FakeBackend({"bad": 1}),
               B.BedrockBackend()):
        e, sink, b = setup(bk, logs=logs, cfg={"infer_pd_bedrock": True})
        e.backing._fixed = bk if not isinstance(bk, B.BedrockBackend) else None
        e.feed(row("t1", KNIFE, 1000))
        e.tick(2000)
    text = "\n".join(logs).lower()
    for w in ("teenage", "destroying", "knife", REASON.lower(), "example ave"):
        assert w not in text


# -- de-escalation -----------------------------------------------------------------------------------------------------

def test_de_escalation_notes_the_open_event_and_pushes_nothing():
    e, sink, b = setup(B.FakeBackend(answer("weapons", 0.9, deescalation=True)), packs=rules_mod.load_packs(None, {}))
    [ev] = e.feed(row("t1", "shots fired at 100 Example Ave", 1000))
    pushes = len(sink.pushes)
    out = e.feed(row("t2", "subject is in custody, the scene is secure, units can slow it down", 1030))
    assert [x.id for x in out] == [ev.id] and e.events.get(ev.id).note == B.NOTE
    assert len(sink.pushes) == pushes and len(e.events.all()) == 1
    assert e.backing.today["notes"] == 1 and e.backing.shown(ev.id)["used"] == "note"


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


def test_the_pc_worker_through_the_hubs_api(monkeypatch):
    assert B.PCWorkerBackend().available() in (True, False)            # the module may not be built here: never raises
    calls = {}

    def submit(task, messages, schema, deadline_s=20, priority=0, meta=False):
        calls.update(task=task, deadline_s=deadline_s, meta=meta, priority=priority, schema=schema)
        return B.resolved((answer(), B.meta("ok", "local-8b", model_version="ab12cd", worker="pc1", latency_ms=900)))
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
    [ev] = e.feed(row("t1", KNIFE, 1000))
    assert calls["task"] == "pd_classify" and calls["deadline_s"] == 15 and calls["meta"] is True
    assert calls["schema"]["additionalProperties"] is False
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
    assert len(e.feed(row("t1", KNIFE, 1000))) == 1


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
    assert post({"action": "set", "key": "infer_pd_bedrock", "value": True}, JORDAN)[0] == 403     # admins only
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


def test_the_record_in_the_store_and_the_more_info_bundle(tmp_path):
    from sizeup import context_bundle
    from tests.test_cad import make_cad
    cad = make_cad(tmp_path)
    clock = Clock(cad.clock())
    b = B.FakeBackend(answer())
    cad.intel.backing = B.ModelBacking(lambda: cad.intel.policy.cfg, store=cad.store, clock=clock, log=lambda m: None,
                                       backend=b)
    cad.intel.locate = example_locate
    cad.intel.packs = police()
    cad.intel.threaded = False
    t = cad.clock()
    out = cad.intel.feed(row("t1", KNIFE, t))
    assert len(out) == 1, out
    r = cad.store.one("SELECT * FROM intel_model")
    assert r["model"] == "fake-1" and r["used"] == "event" and r["event_id"] == out[0].id
    sh = cad.intel.backing.shown(out[0].id)
    assert sh["reason"] == REASON
    from sizeup import sourcekeys
    with sourcekeys.request_context("someone verified"):
        bundle = context_bundle.build(cad, "intel", out[0].id, admin=True)
    assert bundle["why"]["model"]["confidence"] == 0.85 and bundle["why"]["model"]["model"] == "fake-1"
    assert context_bundle.site_lines(cad, "", None, None, t) == []          # no site: no history
    assert context_bundle.site_lines(cad, "100 Example Ave, Port Chester", 41.0, -73.66, t) == []   # no calls there
    assert context_bundle.site_lines(cad, "Example Ave", 41.0, -73.66, t, precision="street") == []


def test_live_the_answer_is_applied_on_the_models_own_thread():
    import threading
    import time as _t
    clock = Clock(1050)
    sink = RecordingSink()
    e = IntelEngine(settings={}, events=MemoryEvents(), sink=sink, locate=example_locate, thread=False, clock=clock,
                    packs=police())
    b = B.FakeBackend(answer(), hold=True)
    seen = []
    e.backing = B.ModelBacking(lambda: e.policy.cfg, clock=clock, log=lambda m: None, backend=b, worker=True)
    orig = e.backing._answered
    e.backing._answered = lambda *a: (seen.append(threading.current_thread().name), orig(*a))[1]
    e.feed(row("t1", KNIFE, 1000))
    t = threading.Thread(target=lambda: b.open[0].set_result((answer(), B.meta("ok"))), name="hub-resolver")
    t.start()
    t.join(5)
    end = _t.time() + 5
    while not e.events.all() and _t.time() < end:
        _t.sleep(0.01)
    assert seen == ["intel-model"] and [x.category for x in e.events.all()] == ["weapons"]
