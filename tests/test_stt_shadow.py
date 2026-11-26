"""Shadow evidence against AWS (#878): an AWS channel (pinned AWS, or Auto · normal when its clip fell to AWS) has AWS
as its champion, so its GPU candidates decode a sampled share of its clips in shadow, AWS's text the reference, under
the challengers' rules (last of all, an idle GPU, taken back by any other work, the hourly caps, never AWS). Scores
are numbers only; identifiers that were in the context sent never count; a recommendation only, never a switch.
Invented clips, units and texts."""

from __future__ import annotations

import json

from sizeup import sttmodels, sttpool, sttscore as S
from sizeup.stt import LIVE
from sizeup.stt_time import Check
from tests.test_channels import fake_aws  # noqa: F401 (the fixture: test credentials only)
from tests.test_stt_models import Meta
from tests.test_stt_pool import Clock

Q8, QWEN, Q5 = "large-v3-turbo-q8_0", "qwen3-asr-1.7b-q8_0", "large-v3-turbo-q5_0"


def test_a_sample_against_aws_and_the_context_never_counts():
    s = S.vs_reference("fire", "Engine 60 and Ladder 31 responding, 1549", "Engine 60 and Ladder 13 responding",
                       (Check("match"), Check("no_time")))
    assert s["ref"] == "aws" and s["ids"] == 0.3333 and s["time"] == [1.0, 0.0] and 0 < s["agree"] < 1
    s = S.vs_reference("fire", "Engine 60 and Ladder 31 responding", "Engine 60 responding", context=["Engine 60"])
    assert s["ids"] == 0.0                                          # (Engine 60 was in its context: not its credit)
    assert S.vs_reference("air", "roger", "roger")["ids"] is None


def test_the_recommendation_needs_enough_clips_and_no_worse_than_aws():
    good = [{"agree": 0.9, "ids": 1.0, "time": [1.0, 1.0]}] * S.MIN_N_SHADOW
    assert S.shadow_summary(good)["verdict"] == f"matches or beats AWS on {S.MIN_N_SHADOW} clips"
    assert S.shadow_summary(good[:-1])["verdict"] == "not yet"
    worse_time = [{"agree": 0.9, "ids": 1.0, "time": [1.0, 0.0]}] * S.MIN_N_SHADOW
    assert not S.shadow_summary(worse_time)["ok"]
    edits = [(0.1, 0.3)] * 10                                       # (people's corrections: AWS closer)
    assert not S.shadow_summary(good, edits)["ok"] and S.shadow_summary(good, edits)["edit"]["n"] == 10


def test_an_aws_channels_candidates_and_its_switch():
    c = sttmodels.Choices(Meta(), clock=Clock())
    assert c.candidates("tg:307", "fire") == [Q5] and c.shadow_on("tg:307")
    c.set("tg:307", {"model": QWEN, "challenger": Q8})
    assert c.candidates("tg:307", "fire") == [QWEN, Q8]
    c.set("tg:307", {"shadow": "off"})
    assert not c.shadow_on("tg:307")
    c.set("tg:307", {"shadow": "on"})
    assert c.shadow_on("tg:307") and "shadow" not in c.settings()["tg:307"]


def test_on_a_hub_an_aws_channels_candidate_runs_in_shadow_and_is_scored_against_aws(tmp_path, fake_aws, monkeypatch):
    from tests.fake_transcribe import FakeTranscribe
    from tests.test_app import wait
    from tests.test_channels import AWS_TEXT, aws_cfg
    from tests.test_cloud import HubRun, call, hub_cfg
    from tests.test_stt_pool import FakeWorker, pending_event, row, send

    monkeypatch.setattr(sttpool, "CHALL_QUIET_S", 0.0)
    monkeypatch.setattr(S, "SHADOW_PCT", 100.0)

    class ModelWorker(FakeWorker):
        def post(self, path, body):
            if path.endswith(("/claim", "/hello", "/state")):
                body = dict(body, models=[Q5, QWEN] if path.endswith("/claim") else body.get("models"),
                            caps=["challenger"])
            elif path.endswith("/complete") and self.jobs:
                body = dict(body, model=self.jobs[-1].get("model") or Q5)
            return call(self.url + path, "POST", body, token=self.token)

    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    app = hub.app
    app._pool_class = lambda item, key: LIVE
    app.stt_choices.set("FG1", {"model": QWEN})
    token = app.hub.workers.add("gpu-1", "Test box")
    w = ModelWorker(hub.url, token, text="Engine 61 on location").start()
    try:
        assert app._hub_engine("FG1") == "pin:aws"                  # (an AWS channel: AWS is its champion)
        app.stt_api.build = {"commit": "c" * 40}
        word = app._stt_models_word()                                # (its GPU model isn't resident: no live use)
        assert QWEN not in word["want"] and not app.sttpool.resident
        send(hub, pending_event("FG1-51"))
        assert wait(lambda: row(hub, "FG1-51").get("text") == AWS_TEXT + ".", 20), row(hub, "FG1-51")
        assert wait(lambda: any(r["role"] == "shadow" for r in app.stt_runs.of("FG1-51")), 30)
        [job] = w.jobs                                              # (the only GPU job: the shadow run)
        assert job["challenger"] is True and job["model"] == QWEN and job["fast"] is False
        run = next(r for r in app.stt_runs.of("FG1-51") if r["role"] == "shadow")
        assert run["input"] == "listening-8k" and run["want"] == QWEN
        assert json.loads(run["score"])["ref"] == "aws"
        assert row(hub, "FG1-51")["text"] == AWS_TEXT + "."         # (AWS's text of record, untouched)
        [board] = app._shadow_boards()
        assert board["key"] == "FG1" and board["on"] and board["models"][0]["model"] == QWEN
        assert board["models"][0]["n"] == 1 and board["models"][0]["verdict"] == "not yet"
        app.stt_choices.set("FG1", {"challenger": Q8})
        word = app._stt_models_word()
        assert Q8 in word["try"] and Q8 not in word["want"]         # (a candidate loaded on demand: never resident)
        assert app._hub_engine("FG1") == "pin:aws"                  # (nothing switches a channel by itself)
    finally:
        w.close()
        hub.close()
        fake.close()
