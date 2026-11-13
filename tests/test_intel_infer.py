"""The inference stage (sizeup/intel/infer.py): only where the rules are unsure, its place only through the gazetteer, its
caps, its record, its provider behind an interface. A fake provider (no network, no AWS); synthetic text only."""

from __future__ import annotations

import json
import sys
import types

import pytest

from sizeup.intel import infer as I
from sizeup.intel.engine import IntelEngine, RecordingSink
from sizeup.intel.events import MemoryEvents
from tests.test_intel import Clock, example_locate, row


class FakeProvider:
    name = "fake"

    def __init__(self, result=None, fail=None):
        self.result = result or I.Result("weapons", 0.9, "intersection", "Sample St and Test Rd", "Shots reported.", 400, 60)
        self.fail = fail
        self.asked: list[I.Request] = []

    def run(self, req):
        self.asked.append(req)
        if self.fail:
            raise self.fail
        return self.result


def setup(cfg=None, provider=None, clock=None):
    clock = clock or Clock(1050)
    settings = {"intel": {"infer": True, **(cfg or {})}}
    sink = RecordingSink()
    e = IntelEngine(settings=settings, events=MemoryEvents(), sink=sink, locate=example_locate, thread=False, clock=clock)
    p = provider or FakeProvider()
    e.infer = I.Inference(lambda: e.policy.cfg, clock=clock, log=lambda m: None, provider=p)
    return e, sink, p


def test_off_by_default():
    assert I.DEFAULTS["infer"] is False and I.DEFAULTS["infer_model"] == "anthropic.claude-haiku-5-5"
    e, sink, p = setup({"infer": False})
    e.feed(row("t1", "report of an MVA on Sample St", 1000))
    assert p.asked == []


def test_only_what_the_rules_leave_uncertain_is_asked():
    e, sink, p = setup(provider=FakeProvider(I.Result("mva_injury", 0.8, "none", "", "Crash with an injury.")))
    e.feed(row("t1", "shots fired at Sample St and Test Rd", 1000))           # sure and placed: never asked
    assert p.asked == []
    e.feed(row("t0", "copy that", 1005))                                     # no hit: never asked
    assert p.asked == []
    evs = e.feed(row("t2", "report of an MVA on Example Ave, caller states someone hurt", 1010))   # 0.3 + cue: unsure
    assert len(p.asked) == 1 and "MVA" in p.asked[0].text
    [ev] = [x for x in evs if x.category == "mva_injury"]
    assert ["infer.bedrock", 1, 0.48] in ev.rules                            # 0.8 x the weight 0.6
    assert any(h.rule_id == "infer.bedrock" and h.event_id == ev.id for h in sink.hits)


def test_its_category_must_be_one_of_the_packs():
    e, sink, p = setup(provider=FakeProvider(I.Result("alien_landing", 0.99, "none", "", "")))
    assert e.feed(row("t1", "report of an MVA on Sample St", 1000)) == []


def test_its_place_is_only_what_the_gazetteer_finds():
    e, sink, p = setup(provider=FakeProvider(I.Result("weapons", 0.9, "intersection", "Sample St and Test Rd", "x")))
    evs = e.feed(row("t1", "shots fired, shots fired", 1000))                # sure, no place: asked for the place
    ev = evs[-1]
    assert len(p.asked) == 1 and len({x.id for x in evs}) == 1 and ev.precision == "intersection" and "place from inference" in ev.basis
    e2, _s, p2 = setup(provider=FakeProvider(I.Result("weapons", 0.9, "address", "1 Nowhere Lane", "x")))
    ev2 = e2.feed(row("t1", "shots fired, shots fired", 1000))[-1]
    assert len(p2.asked) == 1 and ev2.precision == "none"                   # the gazetteer doesn't know it: unused


def test_a_sample_is_recorded_never_acted_on():
    e, sink, p = setup({"infer_sample_pct": 100}, FakeProvider(I.Result("barricade", 0.99, "none", "", "")))
    [ev] = e.feed(row("t1", "shots fired at Sample St and Test Rd", 1000))
    assert len(p.asked) == 1 and ev.category == "weapons" and [r["why"] for r in e.infer.records] == ["sample"]
    assert all(x[0].category != "barricade" for x in sink.events)


def test_the_daily_cap_and_the_monthly_budget():
    e, sink, p = setup({"infer_daily_cap": 2})
    for i in range(4):
        e.feed(row(f"t{i}", f"report of an MVA number {i} on Sample St", 1000 + i))
    assert len(p.asked) == 2 and e.infer.status()["why"] == "daily cap"
    e, sink, p = setup({"infer_budget_usd": 0.0001, "infer_usd_in": 1000.0})
    for i in range(3):
        e.feed(row(f"t{i}", f"report of an MVA number {i} on Sample St", 1000 + i))
    assert len(p.asked) == 1 and e.infer.status()["why"] == "monthly budget"


def test_a_failing_provider_backs_off_and_the_rules_go_on():
    e, sink, p = setup(provider=FakeProvider(fail=RuntimeError("AccessDeniedException")))
    e.feed(row("t1", "report of an MVA on Sample St", 1000))
    e.feed(row("t2", "report of an MVA on Test Rd", 1001))
    assert len(p.asked) == 1 and "AccessDenied" in e.infer.status()["why"]
    assert e.feed(row("t3", "shots fired, shots fired", 1002))               # the rules still raise


def test_what_is_sent_has_no_radio_ids_or_plates_and_no_talkers():
    e, sink, p = setup()
    e.feed(row("t1", "report of an MVA on Sample St, radio 7654321, plate ABC1234", 1000, talker="Officer Example"))
    [req] = p.asked
    assert "7654321" not in req.text and "ABC1234" not in req.text and "Officer" not in req.text
    assert set(req.categories) >= {"weapons", "mva_injury"} and "Port Chester" in req.towns


def test_refusals_and_bad_json_are_nothing():
    assert I.parse("not json").category == "none"
    assert I.parse(json.dumps({"category": "weapons", "confidence": 7})).confidence == 1.0
    e, sink, p = setup(provider=FakeProvider(I.Result(refused=True)))
    assert e.feed(row("t1", "report of an MVA on Sample St", 1000)) == []


def test_the_bedrock_provider_calls_the_mantle_client_with_structured_output(monkeypatch):
    calls = {}

    class Msgs:
        def create(self, **kw):
            calls.update(kw)
            block = types.SimpleNamespace(type="text", text=json.dumps(
                {"category": "hazmat", "confidence": 0.7, "location_kind": "address", "location": "100 Example Ave",
                 "summary": "Gas odor reported."}))
            return types.SimpleNamespace(stop_reason="end_turn", content=[block],
                                         usage=types.SimpleNamespace(input_tokens=300, output_tokens=40))

    class Mantle:
        def __init__(self, aws_region):
            calls["region"] = aws_region
            self.messages = Msgs()
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(AnthropicBedrockMantle=Mantle))
    p = I.PROVIDERS["bedrock"](I.DEFAULTS)
    res = p.run(I.Request("odor of gas at 100 Example Ave", {"hazmat": "Gas / hazmat"}, ["Port Chester"]))
    assert (res.category, res.location, res.tokens_in) == ("hazmat", "100 Example Ave", 300)
    assert calls["model"] == "anthropic.claude-haiku-5-5" and calls["region"] == "us-east-1"
    assert calls["output_config"]["format"]["type"] == "json_schema" and calls["output_config"]["effort"] == "low"
    assert "temperature" not in calls and "thinking" not in calls
    assert "NO person's name" in calls["system"]


def test_settings_turn_it_on_and_set_the_caps(tmp_path):
    from sizeup.cad.api import CadApi
    from tests.test_cad import make_cad
    from tests.test_roles import COREY, JULIAN, call, people
    cad = make_cad(tmp_path)
    people(cad)
    api = CadApi(cad)
    post = lambda b, who=COREY: call(api, "/api/cad/settings/intel", b, who)      # noqa: E731
    assert cad.intel.infer.status()["on"] is False
    assert post({"action": "set", "key": "infer", "value": True}, JULIAN)[0] == 403
    assert post({"action": "set", "key": "infer", "value": True})[0] == 200 and cad.intel.infer.status()["on"]
    assert post({"action": "set", "key": "infer_daily_cap", "value": 50})[0] == 200
    assert post({"action": "set", "key": "infer_model", "value": "us.anthropic.claude-haiku-5-5"})[0] == 200
    assert post({"action": "set", "key": "infer_model", "value": "bad id; drop"})[0] == 400
    assert post({"action": "set", "key": "infer_provider", "value": "openai"})[0] == 400
    st = cad.intel.infer.status()
    assert st["daily_cap"] == 50 and st["model"] == "us.anthropic.claude-haiku-5-5"
    assert cad.store.one("SELECT COUNT(*) AS n FROM intel_infer")["n"] == 0
