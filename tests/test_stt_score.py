"""Champion and challenger scoring (sizeup/sttscore.py): the pinned folding, the identifiers, each domain's metrics
(fire/EMS: the trailing time, the CAD's units and address; police: the channel's terms; airband: the aircraft in
ADS-B), people's edits and identifier recall, the comparison with its interval, the rollback rule and the sampling.
Invented texts, units, callsigns and addresses."""

from __future__ import annotations

import json
import random

from sizeup import sttscore as S
from sizeup.stt import URGENT
from sizeup.stt_time import Check
from tests.test_channels import fake_aws  # noqa: F401 (the fixture: test credentials only)


def test_the_folding_is_pinned():
    assert S.NORM.startswith("norm/2:")
    assert S.normalize("40 Example Rd, 12 Sample St") == ["40", "example", "road", "12", "sample", "street"]
    assert S.found(["40 Example Rd"], "forty Example Road") == {"40 example road"}
    assert S.normalize("Engine Sixty-One, ten four. 77 Alpha 4!") == ["engine", "61", "10-4", "77", "alpha", "4"]
    assert S.normalize("Delta seven six six") == ["delta", "766"]


def test_identifiers():
    ids = S.identifiers("Engine 60 and Ladder thirty one, 77 Alpha 4, ten four, respond to 1250 Example Avenue")
    assert {"engine 60", "ladder 31", "77 alpha 4", "10-4", "1250"} <= ids


def test_word_error_and_agreement():
    assert S.wer("engine sixty on scene", "Engine 60 on scene.") == 0.0
    assert S.wer("", "x") is None
    assert S.agreement("a b c d", "a b x d") == 0.75 and S.agreement("", "") is None


def test_fire_scores_the_trailing_time_and_the_cads_units():
    refs = {"cad": ["Engine 60", "Ladder 31", "1250 Example Avenue"]}
    champ = "Engine 60 Ladder 13 respond 1250 Example Avenue, 1549"
    chall = "Engine 60 Ladder 31 respond 1250 Example Avenue, 1549"
    s = S.sample("ems", champ, chall, refs, (Check("time_mismatch"), Check("match")))
    assert s["domain"] == "fire"
    assert s["chall"]["cad"] == 1.0 and abs(s["champ"]["cad"] - 2 / 3) < 1e-3 and s["champ"]["time"] == 0.0
    assert s["chall"]["score"] > s["champ"]["score"]
    assert 0 < s["agree"] < 1


def test_police_scores_the_channels_terms_and_air_the_aircraft():
    p = S.sample("police", "car twelve to 98 signal nine", "car 12 to 908 signal 9", {"terms": ["signal 9", "908"]})
    assert p["chall"]["terms"] == 1.0 and p["champ"]["terms"] == 0.5
    a = S.sample("air", "delta seven sixty six cleared to land", "Delta 766 cleared to land runway 16",
                 {"aircraft": ["Delta 766", "november 123 alpha bravo"]})
    assert a["champ"]["aircraft"] == a["chall"]["aircraft"] == 1.0        # (folded: "seven sixty six" is 766)
    assert a["domain"] == "air"


def test_an_edit_adds_word_error_and_identifier_recall():
    s = S.sample("fire", "engine 16 on scene", "engine 60 on scene", {}, edit="Engine 60 on scene")
    assert s["chall"]["edit"] == 1.0 and s["champ"]["edit"] == 0.75
    assert s["chall"]["ids"] == 1.0 and s["champ"]["ids"] == 0.0
    again = S.with_edit(S.sample("fire", "engine 16 on scene", "engine 60 on scene", {}), "engine 16 on scene",
                        "engine 60 on scene", "Engine 60 on scene")
    assert again["chall"]["score"] == s["chall"]["score"]


def test_a_clip_where_nothing_applies_is_not_counted():
    s = S.sample("fire", "copy", "copy that", {})
    assert s["champ"]["score"] is None and S.compare([s])["n"] == 0


def test_the_comparison_needs_enough_samples_and_an_interval_clear_of_zero():
    rnd = random.Random(7)

    def mk(a, b):
        return {"agree": 0.9, "champ": {"score": a, "cad": a}, "chall": {"score": b, "cad": b}}
    few = S.compare([mk(0.5, 0.9) for _ in range(5)])
    assert few["n"] == 5 and few["verdict"] == "no difference yet"
    better = S.compare([mk(0.5 + rnd.uniform(-0.05, 0.05), 0.7 + rnd.uniform(-0.05, 0.05)) for _ in range(40)])
    assert better["verdict"] == "challenger better" and better["diff"] > better["ci"] > 0
    assert better["metrics"]["chall"]["cad"]["n"] == 40
    same = S.compare([mk(0.6 + rnd.uniform(-0.2, 0.2), 0.6 + rnd.uniform(-0.2, 0.2)) for _ in range(40)])
    assert same["verdict"] == "no difference yet"


def test_rollback_only_with_enough_samples_a_margin_and_the_interval():
    assert S.should_roll_back({"n": 40, "diff": 0.1, "ci": 0.03})
    assert not S.should_roll_back({"n": 10, "diff": 0.1, "ci": 0.03})
    assert not S.should_roll_back({"n": 40, "diff": 0.01, "ci": 0.005})       # under the margin
    assert not S.should_roll_back({"n": 40, "diff": 0.05, "ci": 0.08})        # the interval holds 0


def test_sampling_is_stable_and_near_its_share():
    ids = [f"clip-{i}" for i in range(5000)]
    picked = [c for c in ids if S.sampled(c, "model-x", 10)]
    assert 400 < len(picked) < 600
    assert picked == [c for c in ids if S.sampled(c, "model-x", 10)]
    assert not any(S.sampled(c, "model-x", 0) for c in ids[:100])


# -- on a hub: sampled, run last on an idle GPU, scored, promoted, rolled back -------------------------------------

def test_a_challenger_runs_on_the_same_clip_is_scored_promoted_and_rolled_back(tmp_path, fake_aws, monkeypatch):
    from sizeup import sttpool, sttscore
    from tests.fake_transcribe import FakeTranscribe
    from tests.test_app import wait
    from tests.test_channels import AWS_TEXT, aws_cfg
    from tests.test_cloud import HubRun, call, hub_cfg
    from tests.test_stt_pool import FakeWorker, pending_event, send

    Q8, QWEN = "large-v3-turbo-q8_0", "qwen3-asr-1.7b-q8_0"
    monkeypatch.setattr(sttpool, "CHALLENGE_QUIET_S", 0.0)

    class ModelWorker(FakeWorker):
        def post(self, path, body):
            if path.endswith("/claim"):
                body = dict(body, models=["large-v3-turbo-q5_0", QWEN, Q8])
            elif path.endswith("/complete") and self.jobs:
                job = self.jobs[-1]
                body = dict(body, model=job.get("model") or "large-v3-turbo-q5_0")
                if job.get("class") == "challenger":
                    body["text"] = "Engine 60 Ladder 31 on location"
            return call(self.url + path, "POST", body, token=self.token)

    fake = FakeTranscribe(phrases={500: AWS_TEXT})
    hub = HubRun(hub_cfg(tmp_path, **aws_cfg(fake)))
    app = hub.app
    app.stt_choices.set("FG1", {"challenger": Q8, "pct": 100})
    token = app.hub.workers.add("gpu-1", "Test box")
    w = ModelWorker(hub.url, token, text="Engine 60 Ladder 13 on location").start()
    try:
        assert wait(lambda: app.sttpool.ready(URGENT)[0], 5)
        send(hub, pending_event("FG1-21"))
        assert wait(lambda: any(r["role"] == "challenger" for r in app.stt_runs.of("FG1-21")), 30)
        classes = [j["class"] for j in w.jobs]
        assert classes[0] != "challenger" and classes[-1] == "challenger" and w.jobs[-1]["model"] == Q8
        assert w.jobs[-1]["prompt"] == w.jobs[0]["prompt"] and w.jobs[-1].get("context") == w.jobs[0].get("context")
        run = [r for r in app.stt_runs.of("FG1-21") if r["role"] == "challenger"][0]
        score = json.loads(run["score"])
        assert score["champion"] == QWEN and score["challenger"] == Q8 and score["norm"] == sttscore.NORM
        assert run["text"] == "Engine 60 Ladder 31 on location"          # kept privately, never on the radio log
        assert app.cad.store.one("SELECT text FROM radio_traffic WHERE id = 'FG1-21'")["text"] != run["text"]
        board = app.challenge_board("FG1")
        assert board["samples"] == 1 and board["champion"] == QWEN and board["challenger"] == Q8
        from sizeup.cad.api import CadApi
        from tests.test_station_identity import CASEY, JORDAN, Identity, people
        people(app.cad)
        api = CadApi(app.cad)
        body = json.dumps({"action": "promote", "key": "FG1", "operator": "Admin Example"}).encode()
        assert api.post("/api/cad/stt-workers", body, Identity(email=JORDAN))[0] == 403      # admins only
        code, out, _ = api.post("/api/cad/stt-workers", body, Identity(email=CASEY))
        assert code == 200, out
        code, raw, _ = api.get("/api/cad/stt-workers", "", Identity(email=CASEY))
        boards = json.loads(raw)["models"]["challenges"]
        assert boards and boards[0]["key"] == "FG1" and "Engine 60 Ladder 31" not in raw.decode()   # scores only
        assert app.stt_choices.model_for("FG1", "fire") == Q8 and app.stt_choices.challenger_for("FG1")[0] == QWEN
        audit = app.cad.store.q("SELECT key, old, new FROM settings_audit WHERE key LIKE 'stt_models/FG1/%'")
        assert {"key": "stt_models/FG1/model", "old": "default", "new": Q8} in audit
        # the model it replaced scores better since: rolled back, audited
        monkeypatch.setattr(app, "challenge_board", lambda key: {"n": 40, "diff": 0.2, "ci": 0.05, "champ": 0.4,
                                                                 "chall": 0.6, "champion": Q8, "challenger": QWEN})
        app._challenge_watch("FG1")
        assert app.stt_choices.model_for("FG1", "fire") == QWEN and app.stt_choices.challenger_for("FG1") is None
        assert app.stt_choices.settings()["FG1"]["rolled_back"]["from"] == Q8
        assert app.cad.store.one("SELECT operator FROM settings_audit WHERE key = 'stt_models/FG1/model' "
                                 "ORDER BY id DESC LIMIT 1")["operator"] == "automatic rollback"
    finally:
        w.close()
        hub.close()
        fake.close()


def test_a_missing_time_costs_as_much_as_a_wrong_one_and_both_sides_share_their_metrics():
    s = S.sample("fire", "Engine 60 responding, 1549", "Engine 60 responding", {"cad": ["Engine 60"]},
                 (Check("time_mismatch"), Check("no_time")))
    assert s["champ"]["time"] == 0.0 and s["chall"]["time"] == 0.0
    s = S.sample("fire", "Engine 60, 1549", "Engine 60", {}, (Check("match"), None))      # (the other not checked)
    assert s["chall"]["time"] is None and s["champ"]["score"] is None                     # nothing both have


def test_a_promotion_is_watched_only_while_its_pair_stands_and_a_new_choice_ends_it():
    from sizeup import sttmodels
    from tests.test_stt_models import Meta
    from tests.test_stt_pool import Clock
    c = sttmodels.Choices(Meta(), clock=Clock())
    c.set("tg:307", {"challenger": "large-v3-turbo-q8_0"})
    c.service_of = lambda k: "fire"
    old, new = c.promote("p25:westchester:307")                                           # (the county's key, as routed)
    assert new["model"] == "large-v3-turbo-q8_0" and new["challenger"] == "qwen3-asr-1.7b-q8_0"
    assert c.promoted("tg:307")["from"] == "qwen3-asr-1.7b-q8_0"
    c.set("tg:307", {"challenger": "large-v3-turbo-q5_0"})
    assert c.promoted("tg:307") == {}                                                     # another pair: no watch


# -- the pool: a challenger never costs a clip of ours ---------------------------------------------------------------

def _challenge_pool():
    from sizeup import sttpool
    from tests.test_stt_pool import Clock, Jobs, online, pool
    clk = Clock()
    p, jobs = pool(clk), Jobs()
    w = online(p, "gpu-a")
    w.models = ["large-v3-turbo-q8_0"]
    return sttpool, clk, p, jobs, w


def test_a_challenger_waits_for_quiet_steps_aside_for_ours_and_never_cools_the_worker():
    sttpool, clk, p, jobs, w = _challenge_pool()
    from sizeup.stt import HIGH, LIVE
    p.heard(HIGH)                                                   # a pre-alert went to AWS: no pool job at all
    assert p.submit(jobs.job("c1~ch", sttpool.CHALLENGE, model="large-v3-turbo-q8_0"))
    assert p.claim("gpu-a", 0) is None                              # not within CHALLENGE_QUIET_S of it
    clk.t += sttpool.CHALLENGE_QUIET_S
    j = p.claim("gpu-a", 0)
    assert j.id == "c1~ch" and j.public()["fast"] is True and j.public()["lease_s"] <= 4.0
    clk.t += 1
    p.heard(LIVE)                                                   # one of ours came: it steps aside at its extend
    assert p.extend("gpu-a", j.lease) is None
    assert p.drain() == 1 and jobs.fell["c1~ch"] == "a clip of ours came" and j.extra["fell"] == "preempted"
    for i in range(sttpool.FAIL_N + 1):                             # challengers handed back again and again ...
        clk.t += sttpool.CHALLENGE_QUIET_S
        assert p.submit(jobs.job(f"x{i}~ch", sttpool.CHALLENGE, model="large-v3-turbo-q8_0"))
        k = p.claim("gpu-a", 0)
        assert p.release("gpu-a", k.lease, "model failed")
    assert w.cool_until <= clk.t and w.fails == 0                   # ... never cool the worker down for our clips
    st = p.stats()
    assert st["queued"] == 0 and st["challengers"]["skipped"] >= sttpool.FAIL_N and st["fallbacks"]["released"] == 0
    assert p.challenges["preempted"] == 1


def test_a_challenger_done_stays_out_of_the_pools_numbers():
    sttpool, clk, p, jobs, w = _challenge_pool()
    assert p.submit(jobs.job("c2~ch", sttpool.CHALLENGE, model="large-v3-turbo-q8_0"))
    j = p.claim("gpu-a", 0)
    assert p.complete("gpu-a", j.lease, {"text": "x", "model": "large-v3-turbo-q8_0"})
    st = p.stats()
    assert st["done_h"] == 0 and st["p50_s"] is None and st["challengers"]["done"] == 1 and w.done == 0
