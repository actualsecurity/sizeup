"""Champion and challenger scoring (sizeup/sttscore.py): the pinned folding, the identifiers, each domain's metrics
(fire/EMS: the trailing time, the CAD's units and address; police: the channel's terms; airband: the aircraft in
ADS-B), people's edits and identifier recall, the comparison with its interval, the rollback rule and the sampling.
Invented texts, units, callsigns and addresses."""

from __future__ import annotations

import random

from sizeup import sttscore as S
from sizeup.stt_time import Check


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
    c.set("tg:307", {"model": "qwen3-asr-1.7b-q8_0", "challenger": "large-v3-turbo-q8_0"})
    c.service_of = lambda k: "fire"
    old, new = c.promote("p25:westchester:307")                                           # (the county's key, as routed)
    assert new["model"] == "large-v3-turbo-q8_0" and new["challenger"] == "qwen3-asr-1.7b-q8_0"
    assert c.promoted("tg:307")["from"] == "qwen3-asr-1.7b-q8_0"
    c.set("tg:307", {"challenger": "large-v3-turbo-q5_0"})
    assert c.promoted("tg:307") == {}                                                     # another pair: no watch


def test_refs_that_were_in_the_context_never_score():
    refs = {"aircraft": ["Delta 766", "JetBlue 12", "N123AB"], "cad": ["Engine 60", "12 Example Road"], "n": 3}
    out = S.drop_context(refs, ["Delta seven six six", "Engine Sixty"], "HPN tower, JetBlue 12.")
    assert out["aircraft"] == ["N123AB"] and out["cad"] == ["12 Example Road"] and out["n"] == 3


def test_a_promote_needs_its_domains_samples_and_the_challenger_better():
    b = {"challenger": "m-b", "champion": "m-a", "domain": "fire", "n": 39, "verdict": "challenger better",
         "champ": 0.5, "chall": 0.6, "diff": 0.1, "ci": 0.03}
    assert S.promote_ok(b) == (False, "39 of 40 samples")
    assert S.promote_ok(dict(b, n=40, verdict="no difference yet"))[1] == "verdict: no difference yet"
    assert S.promote_ok(dict(b, n=40, verdict="champion better"))[0] is False
    ok, said = S.promote_ok(dict(b, n=40))
    assert ok and "m-b 0.600 vs m-a 0.500 over 40 clips" in said
    assert S.promote_ok(dict(b, challenger=""))[0] is False
    assert S.promote_ok(dict(b, domain="other", n=30))[0] is True
