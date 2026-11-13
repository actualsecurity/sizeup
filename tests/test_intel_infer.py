"""The inference stage (sizeup/intel/infer.py): only where the rules are unsure, backing them (never pushing alone), its
place only through the transcript and the gazetteer and never confirmed, its caps and prices, its own thread, its
record made on first use. A fake provider (no network, no AWS); synthetic text only."""

from __future__ import annotations

import json
import sys
import threading
import types

from sizeup.intel import infer as I
from sizeup.intel.engine import IntelEngine, RecordingSink
from sizeup.intel.events import MemoryEvents
from sizeup.intel.model import Place
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


def setup(cfg=None, provider=None, clock=None, locate=example_locate):
    clock = clock or Clock(1050)
    settings = {"intel": {"infer": True, **(cfg or {})}}
    sink = RecordingSink()
    e = IntelEngine(settings=settings, events=MemoryEvents(), sink=sink, locate=locate, thread=False, clock=clock)
    p = provider or FakeProvider()
    e.infer = I.Inference(lambda: e.policy.cfg, clock=clock, log=lambda m: None, provider=p)
    return e, sink, p


def events(e, cat=None):
    return [x for x in e.events.all() if cat is None or x.category == cat]


def test_off_by_default():
    assert I.DEFAULTS["infer"] is False and I.DEFAULTS["infer_model"] == "anthropic.claude-haiku-5-5"
    e, sink, p = setup({"infer": False})
    e.feed(row("t1", "report of an MVA on Sample St", 1000))
    assert p.asked == []


def test_only_what_the_rules_leave_uncertain_is_asked_and_agreement_may_push():
    e, sink, p = setup(provider=FakeProvider(I.Result("mva_injury", 0.8, "none", "", "Crash with an injury.")))
    e.feed(row("t1", "shots fired at Sample St and Test Rd", 1000))           # sure and placed: never asked
    e.feed(row("t0", "copy that", 1005))                                     # no hit: never asked
    assert p.asked == []
    e.feed(row("t2", "report of an MVA on Example Ave, caller states someone hurt", 1010))   # 0.3 + cue: unsure
    assert len(p.asked) == 1 and "MVA" in p.asked[0].text
    [ev] = events(e, "mva_injury")
    assert ["infer.bedrock", 1, 0.48] in ev.rules and ev.mode == "push"     # the model AGREES with the rules
    assert any(d.would_push for x, d in sink.pushes if x.category == "mva_injury")


def test_the_model_alone_never_pushes():
    # the rules are unsure of an MVA; the model says weapons (no rule said so): map only, never a push
    e, sink, p = setup({"infer_weight": 1.0}, FakeProvider(I.Result("weapons", 0.95, "none", "", "x")))
    e.feed(row("t1", "report of an MVA on Example Ave", 1000))
    [ev] = events(e, "weapons")
    assert ev.mode == "map" and not any(d.would_push for _x, d in sink.pushes)
    e2, sink2, _p = setup({"infer_agree_push": False}, FakeProvider(I.Result("mva_injury", 0.95, "none", "", "")))
    e2.feed(row("t1", "report of an MVA on Example Ave, caller states someone hurt", 1000))
    assert [x.mode for x in events(e2, "mva_injury")] == ["map"]              # the switch: agreement held too


def test_the_category_is_an_enum_of_the_packs():
    s = I.schema(["weapons", "fire_report"])
    assert s["properties"]["category"]["enum"] == ["weapons", "fire_report", "none"]
    e, sink, p = setup(provider=FakeProvider(I.Result("alien_landing", 0.99, "none", "", "")))
    e.feed(row("t1", "report of an MVA on Sample St", 1000))
    assert events(e) == []


def test_its_place_must_be_said_found_and_is_never_confirmed():
    e, sink, p = setup(provider=FakeProvider(I.Result("weapons", 0.9, "intersection", "Sample St and Test Rd", "x")))
    e.feed(row("t1", "shots fired, shots fired", 1000))                     # the model's place: not in the words
    [ev] = events(e, "weapons")
    assert len(p.asked) == 1 and ev.precision == "none"

    def loc(t, s):
        return Place("address", 41.0, -73.66, 25.0, "100 Example Ave, Port Chester", "Port Chester", True, 0.9) \
            if t.startswith("100") else None
    e, sink, p = setup(provider=FakeProvider(I.Result("weapons", 0.9, "address", "100 Example Ave", "x")), locate=loc)
    e.feed(row("t1", "shots fired, shots fired, over by Example Ave", 1000))
    [ev] = events(e, "weapons")
    assert ev.precision == "address" and not ev.confirmed and "place from inference" in ev.basis
    bodies = [d.message["body"] for _x, d in sink.pushes if d.would_push]
    assert bodies and "100 Example Ave" not in bodies[-1] and "Example Ave, 100 blk" in bodies[-1]   # street and block


def test_it_never_replaces_a_place_the_rules_confirmed():
    e, sink, p = setup(provider=FakeProvider(I.Result("weapons", 0.9, "address", "Quarry Ln", "x")))
    e.feed(row("t1", "shots fired at Sample St and Test Rd, Quarry Ln", 1000))
    [ev] = events(e, "weapons")
    assert ev.label_full.startswith("Sample St") and ev.confirmed and p.asked == []


def test_a_sample_is_recorded_never_acted_on():
    e, sink, p = setup({"infer_sample_pct": 100}, FakeProvider(I.Result("barricade", 0.99, "none", "", "")))
    e.feed(row("t1", "shots fired at Sample St and Test Rd", 1000))
    assert len(p.asked) == 1 and [r["why"] for r in e.infer.records] == ["sample"]
    assert events(e, "barricade") == []


def test_the_place_is_asked_about_once_per_event_per_ten_minutes():
    clock = Clock(1000)
    e, sink, p = setup(provider=FakeProvider(I.Result("weapons", 0.9, "none", "", "")), clock=clock)
    for k in range(4):
        clock.t = 1000 + k * 60
        e.feed(row(f"t{k}", f"shots fired, shots fired number {k}", clock.t))
    assert len(p.asked) == 1
    clock.t = 1000 + I.PLACE_REASK_S + 1
    e.feed(row("t9", "more shots fired, shots fired", clock.t))
    assert len(p.asked) == 2


def test_the_daily_cap_and_the_monthly_budget():
    e, sink, p = setup({"infer_daily_cap": 2}, FakeProvider(I.Result("none", 0.0, "none", "", "")))
    for i in range(4):
        e.feed(row(f"t{i}", f"report of an MVA number {i} on Sample St", 1000 + i, source=f"p25:mrrs:92{i}"))
    assert len(p.asked) == 2 and e.infer.status()["why"] == "daily cap"
    e, sink, p = setup({"infer_budget_usd": 0.0001, "infer_usd_in": 1000.0, "infer_usd_out": 1000.0},
                       FakeProvider(I.Result("none", 0.0, "none", "", "", 400, 60)))
    for i in range(3):
        e.feed(row(f"t{i}", f"report of an MVA number {i} on Sample St", 1000 + i, source=f"p25:mrrs:92{i}"))
    assert len(p.asked) == 1 and e.infer.status()["why"] == "monthly budget"


def test_an_unknown_model_without_a_price_is_refused():
    e, sink, p = setup({"infer_model": "some.other-model"})
    e.feed(row("t1", "report of an MVA on Sample St", 1000))
    assert p.asked == [] and "no price" in e.infer.status()["why"]
    assert I.price({"infer_model": "anthropic.claude-haiku-5-5", "infer_usd_in": None, "infer_usd_out": None}) == (0.10, 0.50)


def test_a_failing_provider_backs_off_and_the_rules_go_on():
    e, sink, p = setup(provider=FakeProvider(fail=RuntimeError("AccessDeniedException")))
    e.feed(row("t1", "report of an MVA on Sample St", 1000))
    e.feed(row("t2", "report of an MVA on Test Rd", 1001, source="p25:mrrs:9299"))
    assert len(p.asked) == 1 and "AccessDenied" in e.infer.status()["why"]
    assert e.feed(row("t3", "shots fired, shots fired", 1002, source="p25:mrrs:9298"))


def test_a_hung_call_never_holds_the_engine():
    gate = threading.Event()

    class Hung(FakeProvider):
        def run(self, req):
            gate.wait(5)
            return super().run(req)
    e, sink, p = setup(provider=Hung(I.Result("mva_injury", 0.8, "none", "", "")))
    e.infer.worker = True
    e.feed(row("t1", "report of an MVA on Example Ave, caller states someone hurt", 1000))   # queued, not waited on
    assert e.feed(row("t2", "shots fired, shots fired", 1001, source="p25:mrrs:9299"))       # the engine goes on
    gate.set()
    assert e.infer.flush(5)
    assert events(e, "mva_injury")
    assert I.CALL_TIMEOUT_S <= 15


def test_what_is_sent_has_no_radio_ids_or_plates_and_no_talkers():
    e, sink, p = setup()
    e.feed(row("t1", "report of an MVA on Sample St, radio 7654321, plate ABC1234, ABC 1234", 1000, talker="Officer Example"))
    [req] = p.asked
    assert "7654321" not in req.text and "ABC1234" not in req.text and "ABC 1234" not in req.text
    assert "Officer" not in req.text and set(req.categories) >= {"weapons", "mva_injury"} and "Port Chester" in req.towns


def test_refusals_and_bad_json_are_nothing():
    assert I.parse("not json").category == "none"
    assert I.parse(json.dumps({"category": "weapons", "confidence": 7})).confidence == 1.0
    e, sink, p = setup(provider=FakeProvider(I.Result(refused=True)))
    e.feed(row("t1", "report of an MVA on Sample St", 1000))
    assert events(e) == []


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
        def __init__(self, aws_region, timeout, max_retries):
            calls.update(region=aws_region, timeout=timeout, max_retries=max_retries)
            self.messages = Msgs()
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(AnthropicBedrockMantle=Mantle))
    p = I.PROVIDERS["bedrock"](I.DEFAULTS)
    res = p.run(I.Request("odor of gas at 100 Example Ave", {"hazmat": "Gas / hazmat"}, ["Port Chester"]))
    assert (res.category, res.location, res.tokens_in) == ("hazmat", "100 Example Ave", 300)
    assert calls["model"] == "anthropic.claude-haiku-5-5" and calls["region"] == "us-east-1"
    assert calls["timeout"] == I.CALL_TIMEOUT_S and calls["max_retries"] == 0
    fmt = calls["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["properties"]["category"]["enum"] == ["hazmat", "none"]
    assert calls["output_config"]["effort"] == "low" and "temperature" not in calls and "thinking" not in calls


def test_settings_turn_it_on_refuse_unpriced_models_and_no_table_at_start(tmp_path):
    from sizeup.cad.api import CadApi
    from tests.test_cad import make_cad
    from tests.test_roles import COREY, JULIAN, call, people
    cad = make_cad(tmp_path)
    assert cad.store.one("SELECT name FROM sqlite_master WHERE name = 'intel_infer'") is None     # never at start
    people(cad)
    api = CadApi(cad)
    post = lambda b, who=COREY: call(api, "/api/cad/settings/intel", b, who)      # noqa: E731
    assert cad.intel.infer.status()["on"] is False
    assert post({"action": "set", "key": "infer", "value": True}, JULIAN)[0] == 403
    assert post({"action": "set", "key": "infer", "value": True})[0] == 200 and cad.intel.infer.status()["on"]
    assert post({"action": "set", "key": "infer_daily_cap", "value": 50})[0] == 200
    assert post({"action": "set", "key": "infer_model", "value": "us.anthropic.claude-haiku-5-5"})[0] == 200
    code, out = post({"action": "set", "key": "infer_model", "value": "some.unpriced-model"})
    assert code == 400 and "price" in out["error"]
    assert post({"action": "set", "key": "infer_model", "value": "bad id; drop"})[0] == 400
    assert post({"action": "set", "key": "infer_provider", "value": "openai"})[0] == 400
    assert post({"action": "set", "key": "infer_alone_mode", "value": "log"})[0] == 200
    st = cad.intel.infer.status()
    assert st["daily_cap"] == 50 and st["model"] == "us.anthropic.claude-haiku-5-5"
    assert cad.store.one("SELECT name FROM sqlite_master WHERE name = 'intel_infer'") is None
