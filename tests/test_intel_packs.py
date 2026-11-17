"""The log-only intel packs (data/intel/county_police.json, ems.json, fire.json) and the speech-to-text table their
phrases are matched through (data/intel/tables/stt.json, sizeup/intel/rules.py Normalizer).

What is held here:
  - the three packs, every category and every rule of them compile at "log": nothing of theirs reaches the notifier,
    the map (GET /api/cad/intel/events) or the console's card, whatever the Settings of the pack before them say;
    only an admin raising the pack AND the category AND the rule does;
  - their scope is by service and system, as main names them (never a talkgroup number);
  - the rules: what raises and what doesn't, per pack;
  - the table: sound-alikes, split and merged words, number words; it only ever adds a reading, and the first pack
    (which has no table) matches exactly as before;
  - a push from one of these packs, once an admin has raised it, carries no person's name and no transcript words.

Synthetic only (this repository is public): invented, short, fragmentary radio text with invented mishearings,
"Example Ave" / "Sample St" / "Test Rd", made-up talkers. No real transcripts, addresses, names or topics."""

from __future__ import annotations

import json
import time

import pytest

from sizeup.cad.api import CadApi
from sizeup.intel import rules as rules_mod
from sizeup.intel.engine import IntelEngine, LiveSink, RecordingSink
from sizeup.intel.events import MemoryEvents
from sizeup.intel.model import Place
from tests.test_cad import make_cad
from tests.test_intel import Clock, FakeNotifier, counts, example_locate, get
from tests.test_roles import CASEY, JORDAN, VAL, call, people

NEW = ("county_police", "ems", "fire")
CPD, FIRE_TG, FG = "p25:westchester:363", "p25:westchester:307", "fm:FG1"       # talkgroups of the public talkgroups file


def crow(tid, text, at, source=CPD, service="police", **kw):
    system = "analog" if source.startswith("fm:") else "airband" if source.startswith("air:") else source.split(":")[1]
    r = {"id": tid, "start": at, "end": at + 3, "source": source, "system": system, "service": service, "dept": "",
         "tier": "", "talkgroup": None if ":" not in source[4:] else int(source.rsplit(":", 1)[1]), "channel": "",
         "talker": "", "rig": "", "radio_id": "", "text": text, "alt_text": "", "model": "whisper"}
    r.update(kw)
    return r


def engine(settings=None, clock=None, locate=None):
    sink = RecordingSink()
    return IntelEngine(settings=settings or {}, events=MemoryEvents(), sink=sink, locate=locate, thread=False,
                       clock=clock or Clock()), sink


def raised(text, source, service, settings=None):
    """The (pack, category) of every event one transmission raises, and the sink."""
    e, sink = engine(settings)
    evs = e.feed(crow("t1", text, 1000.0, source, service))
    return {(ev.pack, ev.category) for ev in evs}, sink


def everything_at(mode: str, names=NEW) -> dict:
    """Settings that raise these packs, every category and every rule of them to `mode` (what an admin would do)."""
    packs = [p for p in rules_mod.load_packs() if p.name in names]
    return {"intel_packs": {p.name: {"mode": mode, "categories": {c: {"mode": mode} for c in p.categories}} for p in packs},
            "intel_rules": {r.id: {"mode": mode} for p in packs for r in p.rules}}


# -- the packs as shipped: log only, scoped by service ---------------------------------------------------------------

def test_the_new_packs_the_categories_and_the_rules_are_all_at_log():
    packs = {p.name: p for p in rules_mod.load_packs()}
    assert list(packs) == ["police", *NEW]
    for name in NEW:
        p = packs[name]
        assert p.mode == "log" and p.accept is False and p.norm is not None, name
        assert p.categories and all(c["mode"] == "log" for c in p.categories.values()), name
        assert len(p.rules) >= 15 and all(r.mode == "log" and r.enabled for r in p.rules), name
        raw = json.loads((rules_mod.DATA / f"{name}.json").read_text())
        # written out in the file, never left to a default (a default of "push" must not turn one on)
        assert raw["mode"] == "log" and all(c.get("mode") == "log" for c in raw["categories"].values()), name
        assert all(r.get("mode") == "log" for r in raw["rules"]), name
        # a strong rule of its own in every category: at or over the threshold alone (what a later always-alert marks);
        # "EMS requested" is the one category that only ever adds up (the request and what it is for)
        for cat in set(p.categories) - {"ems_call"}:
            assert any(r.category == cat and r.w >= p.threshold for r in p.rules), (name, cat)
    ids = [r.id for p in packs.values() for r in p.rules]
    assert len(ids) == len(set(ids))
    for name, prefix in (("county_police", "cpd."), ("ems", "ems."), ("fire", "fire.")):
        assert all(r.id.startswith(prefix) for r in packs[name].rules)
    labels = [c["label"] for p in packs.values() for c in p.categories.values()]
    assert len(labels) == len(set(labels))                # the Settings card names each category's control by its label


def test_the_new_packs_and_the_table_have_no_real_strings():
    for f in [*(rules_mod.DATA / f"{n}.json" for n in NEW), rules_mod.TABLES / "stt.json"]:
        raw = f.read_text().lower()
        for s in ("ntfy", "pcfd-", "officer ", "@", "http", "tg:"):
            assert s not in raw, (f.name, s)
    for name in NEW:                                      # scope: source-key globs and services, never a number
        sc = json.loads((rules_mod.DATA / f"{name}.json").read_text())["scope"]
        assert sc["services"] and all(g.endswith(":*") for g in sc["sources"]), name


@pytest.mark.parametrize("source, service, want", [
    (CPD, "police", {"county_police"}),                   # a county police talkgroup
    ("p25:westchester:383", "police", {"county_police"}),
    ("p25:mrrs:9211", "police", {"police"}),              # the first pack's own system: its scope is unchanged
    (FIRE_TG, "fire", {"ems", "fire"}),                   # a county Fire/EMS talkgroup: fire chatter and EMS
    ("p25:westchester:301", "fire", {"ems", "fire"}),
    (FG, "fire", {"fire"}),                               # our analog fire channels: fire chatter only
    ("fm:South 2", "fire", {"fire"}),
    ("p25:mrrs:9570", "ems", {"ems"}),                    # another system's EMS talkgroup
    ("p25:westchester:2103", "ems", {"ems"}),
    ("air:HPN Tower", "air", set()),                      # nothing reads the airband, transit or the rest
    ("p25:westchester:1325", "transit", set()),
    ("p25:westchester:339", "other", set()),
    ("p25:mrrs:9121", "transit", set()),
])
def test_scope_is_by_service_and_system(source, service, want):
    row = crow("t1", "anything", 1000.0, source, service)
    assert {p.name for p in rules_mod.load_packs() if p.in_scope(row)} == want


def test_the_first_pack_is_untouched_by_the_table():
    pol = rules_mod.load_packs()[0]
    assert pol.name == "police" and pol.norm is None and pol.mode == "push"
    assert all(r.norm is None for r in pol.rules)
    assert raised("order of gas in the basement", "p25:mrrs:9211", "police")[0] == set()        # as before: no table
    assert raised("odor of gas in the basement", "p25:mrrs:9211", "police")[0] == {("police", "hazmat")}
    solo = rules_mod.load_packs([json.loads((rules_mod.DATA / "police.json").read_text())])
    assert "norm" not in json.dumps([[p.name, p.version] for p in solo])
    assert rules_mod.ruleset_hash(solo) == rules_mod.ruleset_hash([pol])


# -- the speech-to-text table ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("heard, meant", [
    ("order of gas outside", "odor of gas outside"),                        # the sound-alike, with its context
    ("strong oder of natural gas", "strong odor of natural gas"),
    ("place an order for lunch", None),                                     # "order" alone is left alone
    ("vehicle roll over", "vehicle rollover"),                              # split words
    ("starting c p r now", "starting cpr now"),
    ("E.M.S. in route", "ems en route"),                                    # (words() reads E.M.S. as one word already)
    ("e m s is enroute", "ems is en route"),                                # merged words
    ("carbonmonoxide alarm", "carbon monoxide alarm"),
    ("may day may day", "mayday mayday"),
    ("transmit a second alarm", "transmit a 2nd alarm"),                    # number words as digits
    ("two car m v a", "2 car mva"),
    ("ten seventy five the box", "10 75 the box"),                          # the longest entry first
    ("f d is around for a fire alarm", "fd is en route for a fire alarm"),  # forms seen after the replay
    ("c p r on progress", "cpr in progress"),
    ("two car m b a", "2 car mva"),
    ("portchester mutual aid", "port chester mutual aid"),
    ("copy that headquarters", None),
    ("", None),
])
def test_the_table_fixes_sound_alikes_split_words_and_numbers(heard, meant):
    n = rules_mod.table("stt")
    got = n.apply(rules_mod.words(heard))
    assert (None if got is None else " ".join(got)) == meant
    assert n.apply(rules_mod.words(heard)) == got                           # the same answer again (its one-entry cache)


def test_a_fix_only_ever_adds_a_reading():
    n = rules_mod.Normalizer({"sound": {"order of gas": "odor of gas"}, "numbers": {"second": "2nd"}}, "t")
    d = {"name": "t", "normalize": "stt", "categories": {"c": {}},
         "rules": [{"id": "t.raw", "category": "c", "any": ["second alarm"]}, {"id": "t.fixed", "category": "c", "any": ["2nd alarm"]},
                   {"id": "t.not", "category": "c", "any": ["alarm"], "not": ["2nd alarm"]},
                   {"id": "t.re", "category": "c", "any": ["re:\\bodor of gas\\b"]}]}
    p = rules_mod.compile_pack(d)
    hit = lambda text: {r.id for r in p.rules if r.hit(text, rules_mod.words(text))}      # noqa: E731
    # each reading on its own: as transcribed, t.not's "alarm" is found and its "2nd alarm" isn't, so it hits; the
    # table's reading can't take that away
    assert hit("transmit a second alarm") == {"t.raw", "t.fixed", "t.not"}
    assert hit("transmit a 2nd alarm") == {"t.fixed"}                       # its "not" guards the reading it was found in
    assert hit("fire alarm") == {"t.not"}
    assert hit("order of gas") == {"t.re"}                                  # an expression too, on the fixed words
    assert n.apply(["order", "of", "gas"]) == ["odor", "of", "gas"] and n.apply(["order", "up"]) is None
    assert n.apply(["second", "second"]) == ["2nd", "2nd"]                  # one pass: what a fix wrote is not fixed again
    d2 = dict(d, normalize="")
    plain = rules_mod.compile_pack(d2)
    said = "transmit a second alarm"                                        # without the table: the words as transcribed only
    assert {r.id for r in plain.rules if r.hit(said, rules_mod.words(said))} == {"t.raw", "t.not"}
    assert rules_mod.ruleset_hash([p]) != rules_mod.ruleset_hash([plain])   # the table is part of what decides a hit


def test_the_table_never_takes_a_hit_away_or_beats_a_negation():
    """The review's two cases: "first alarm" read as "1st alarm" (a "not" phrase of fire.alarm_level) must not remove
    the hit the words as transcribed have; and "zero injuries" must not become a number that a gap may cross."""
    def hits(text, source=FG, service="fire"):
        e, sink = engine()
        e.feed(crow("t1", text, 1000.0, source, service))
        return {h.rule_id for h in sink.hits}
    assert "fire.alarm_level" in hits("transmit the second alarm, first alarm companies stay")
    assert "fire.alarm_level" in hits("transmit the 2nd alarm first alarm companies")
    assert "fire.alarm_level" not in hits("no second alarm")                # the fixed reading's own "not" still guards it
    for text in ("accident with zero injuries", "crash zero injured", "m v a with zero injuries"):
        assert raised(text, CPD, "police")[0] == set(), text
        assert not {"cpd.mva_injury"} & hits(text, CPD, "police"), text
    assert rules_mod.table("stt").apply(["zero"]) is None


def test_with_the_table_the_rules_hit_are_a_superset_of_those_without():
    """A property, over many generated sentences: whatever the table does, every rule that hits the words as
    transcribed still hits (a fix adds readings, never takes one away)."""
    import random
    tab = rules_mod.table("stt")
    vocab = sorted({w for k, v in tab.map.items() for w in k + v} | set(rules_mod.NEGATORS)
                   | {"alarm", "fire", "smoke", "gas", "odor", "accident", "injuries", "ems", "en", "route", "not", "breathing",
                      "elevator", "stuck", "car", "water", "mayday", "units", "additional", "engine", "the", "a", "at",
                      "Example", "Ave", "100", "for", "with", "from", "in", "on", "shots", "fired", "pd", "fd"})
    phrase_bits = [" ".join(k) for k in tab.map] + ["odor of gas", "2nd alarm", "1st alarm", "no injuries", "not breathing"]
    rnd = random.Random(7)
    packs = [p for p in rules_mod.load_packs() if p.norm is not None]
    plain = rules_mod.load_packs([{**p.src, "normalize": ""} for p in packs])
    checked = added = 0
    for _ in range(3000):
        parts = [rnd.choice(vocab) if rnd.random() < 0.6 else rnd.choice(phrase_bits) for _ in range(rnd.randint(1, 9))]
        text = " ".join(parts)
        ws = rules_mod.words(text)
        for p, q in zip(packs, plain):
            with_t = {r.id for r in p.rules if r.hit(text, ws)}
            without = {r.id for r in q.rules if r.hit(text, ws)}
            assert without <= with_t, (text, without - with_t)
            checked += 1
            added += len(with_t - without)
    assert checked == 3000 * len(packs) and added > 0                        # (and the table does add hits)


@pytest.mark.parametrize("table, why", [
    ({"numbers": {"zero": "0"}}, "negation"),
    ({"sound": {"no breathing": "breathing"}}, "negation"),
    ({"sound": {"a b c d e f g": "x"}}, "words"),
    ({"sound": {"": "x"}}, "words"),
    ({"sound": ["a"]}, "object"),
    ({"sound": {"a": "b"}, "split": {"a": "c"}}, "twice"),
])
def test_a_bad_table_says_why(table, why):
    with pytest.raises(ValueError, match=why):
        rules_mod.Normalizer(table, "t")


def test_a_pack_naming_no_such_table_does_not_compile():
    d = {"name": "t", "normalize": "nope", "categories": {"c": {}}, "rules": []}
    with pytest.raises(ValueError, match="no data/intel/tables/nope.json"):
        rules_mod.compile_pack(d)
    with pytest.raises(ValueError, match="table's name"):
        rules_mod.compile_pack(dict(d, normalize="../police"))


# -- what raises and what doesn't --------------------------------------------------------------------------------------

COUNTY_PD = [
    ("order of gas outside the building", "hazmat"),                        # the mishearing the first pack never matched
    ("strong oder of natural gas", "hazmat"),
    ("wires down and arcing on Example Ave", "hazmat"),
    ("carbonmonoxide alarm going off", "hazmat"),
    ("he's not breathing", "aided"),
    ("starting c p r", "aided"),
    ("female un responsive in the lobby", "aided"),
    ("possible over dose narcan given", "aided"),
    ("respond to 100 Example Ave for a fall e m s in route", "ems_call"),
    ("seizure in the lobby ambulance requested", "ems_call"),
    ("vehicle roll over on Sample St", "mva_injury"),
    ("pedestrian struck Sample St and Test Rd", "mva_injury"),
    ("car into a building", "mva_injury"),
    ("accident with injuries Example Ave", "mva_injury"),
    ("pursuit terminated suspect vehicle crashed", "mva_injury"),
    ("vehicle drove into the pond", "water_rescue"),
    ("somebody in the river", "water_rescue"),
    ("smoke coming from the roof", "fire_report"),
    ("the truck is on fire", "fire_report"),
    ("for a fire alarm at the school", "alarm"),
    ("alarm going off fire notified", "fd_call"),
    ("have the fire department respond", "fd_call"),
    ("stuck elevator with two people inside", "rescue"),
    ("child locked in the car", "rescue"),
    ("shots fired", "weapons"),
    ("male with a knife", "weapons"),
    ("barricaded subject inside the house", "barricade"),
    ("male armed with a knife on Sample St", "weapons"),
    # after the replay: the kinds of phrasing and mishearing it showed, in invented sentences
    ("e m s en route to Sample St", "ems_call"),
    ("ambulance is being sent to 100 Example Ave", "ems_call"),
    ("70 year old male with chest pain", "ems_call"),
    ("adult female not feeling well", "ems_call"),
    ("f d is around for a water leak at 100 Example Ave", "fd_call"),
    ("fire department is being dispatched to the school", "fd_call"),
    ("for a residential c o alarm", "hazmat"),
    ("two car m b a with injuries", "mva_injury"),
    ("c p r on progress", "aided"),
]
COUNTY_PD_QUIET = [
    # the review's false-alarm candidates (#535), invented sentences
    "co detector low battery chirping", "natural gas truck blocking the lane", "gas company crew on Sample St",
    "vehicle parked in the fire lane", "he's armed with a cell phone",
    "copy", "10-4 headquarters", "barricade", "road barricades are up on Sample St", "traffic stop Sample St",
    "he ran out of gas", "gas station on Example Ave", "burglar alarm commercial", "smoke detector chirping",
    "no injuries property damage only accident", "the fire department is on scene",
    "m v a Sample St", "cancel the ambulance patient refused", "false alarm burnt food", "fire alarm test at the school",
    "place an order for lunch", "elevator is empty nobody inside", "nobody in the water", "not armed",
    "disregard the smoke coming from the roof", "for training shots fired",
    "we don't need e m s anymore", "dog hit by a car on Sample St", "she fainted but is alert again", "elderly male walking a dog",
    "negative overdose", "are there any injuries with that crash", "armed one copy",
    "just smoke from cooking", "female unresponsive now alert",
]
EMS = [
    ("unresponsive male c p r in progress", "aided"),
    ("patient is not breathing", "aided"),
    ("possible overdose", "aided"),
    ("roll over with entrapment", "mva_injury"),
    ("pedestrian struck by a car", "mva_injury"),
    ("m v a on the parkway", "mva_injury"),                                 # the word and the road together
    ("three patients need another ambulance", "mci"),
    ("requesting the fire department for a lift assist", "fd_call"),
    ("need manpower for forcible entry", "fd_call"),
    ("smoke inhalation one patient", "fire_report"),
    ("two patients sick carbon monoxide", "hazmat"),
    ("worker in contact with live wires", "hazmat"),
    ("pulled a child from the pool", "water_rescue"),
    ("stuck in the elevator", "rescue"),
    ("fell from a ladder", "rescue"),
    ("scene is not safe stage for p d", "weapons"),
    ("patient shot", "weapons"),
]
EMS_QUIET = [
    "staging for p d at the corner",
    "ambulance responding to 10 Sample St for a fall", "responding", "m v a Sample St", "patient refusal r m a",
    "scene is safe", "transporting one to the hospital", "structure fire Example Ave", "odor of gas in the basement",
    "requesting an additional engine", "partial roof collapse", "this is a drill c p r in progress", "back in service",
    "you're not responding", "clear of the lift assist", "medic 4 patients are stable",
    "no c o readings, patient feels sick", "holding for police to bring the keys", "going out of service for decon",
]
FIRE = [
    ("may day may day may day", "mayday"),
    ("firefighter down second floor", "mayday"),
    ("member down on the second floor", "mayday"),
    ("command to 60 control transmit a second alarm", "more_units"),
    ("requesting an additional engine and a ladder", "more_units"),
    ("special call a tanker", "more_units"),
    ("all units evacuate the building", "evacuate"),
    ("we're going defensive", "evacuate"),
    ("everybody out", "evacuate"),
    ("partial roof collapse on the bravo side", "collapse"),
    ("we lost water", "water_supply"),
    ("dead hydrant need another water supply", "water_supply"),
    ("requesting mutual aid from Port Chester for a fast team", "mutual_aid"),
    ("Rye Brook to cover our headquarters", "mutual_aid"),
    ("working fire heavy smoke showing", "working_fire"),
    ("ten seventy five the box", "working_fire"),
    ("reports of people trapped on the third floor", "trapped"),
    ("we'll go with the 1075", "working_fire"),
    ("responding on a 10-75", "working_fire"),
    ("portchester mutual aid to cover", "mutual_aid"),
    ("command to 60 control requesting additional manpower", "more_units"),
]
FIRE_QUIET = [
    "a second alarm from the alarm company", "second alarm activation at the same address", "member down the block",
    "Port Chester engine to standby at the game", "additional engine for the parade",
    "low water pressure in the area per the water department", "out of water in the booster tank, refilling",
    "engine 1 responding", "radio check", "this is a drill mayday mayday", "mayday drill tonight", "fire is under control",
    "patient collapsed in the kitchen", "no additional units needed", "hydrant test complete", "mutual aid",
    "nothing showing investigating", "everyone is out of the building", "family member missing", "collapse zone",
    "smoke showing",
    "engine 2 ladder 3 rescue 4 responding", "responding to 1075 Example Ave", "negative fire showing",
    "we don't need any additional engines", "additional manpower", "low water pressure alarm at the mill",
    "port chester mutual aid was cancelled", "engine 9 has the hydrant",
]


@pytest.mark.parametrize("text, cat", COUNTY_PD)
def test_county_police_raises(text, cat):
    got, sink = raised(text, CPD, "police")
    assert ("county_police", cat) in got, got
    assert {p for p, _c in got} == {"county_police"} and sink.pushes and not any(d.would_push for _e, d in sink.pushes)


@pytest.mark.parametrize("text", COUNTY_PD_QUIET)
def test_county_police_stays_quiet(text):
    assert raised(text, CPD, "police")[0] == set()


@pytest.mark.parametrize("text, cat", EMS)
def test_ems_raises(text, cat):
    got, sink = raised(text, FIRE_TG, "fire")
    assert ("ems", cat) in got, got
    assert not any(d.would_push for _e, d in sink.pushes)
    assert ("ems", cat) in raised(text, "p25:mrrs:9570", "ems")[0]          # and on an EMS talkgroup of another system


@pytest.mark.parametrize("text", EMS_QUIET)
def test_ems_stays_quiet(text):
    assert {c for p, c in raised(text, FIRE_TG, "fire")[0] if p == "ems"} == set()


@pytest.mark.parametrize("text, cat", FIRE)
def test_fire_raises(text, cat):
    for source in (FIRE_TG, FG):                                            # a county talkgroup and an analog fireground
        got, sink = raised(text, source, "fire")
        assert ("fire", cat) in got, (source, got)
        assert not any(d.would_push for _e, d in sink.pushes)


@pytest.mark.parametrize("text", FIRE_QUIET)
def test_fire_stays_quiet(text):
    assert {c for p, c in raised(text, FG, "fire")[0] if p == "fire"} == set()


def test_a_crash_and_the_ambulance_asked_for_in_the_next_transmission_are_one_event():
    e, sink = engine()
    assert e.feed(crow("t1", "two car m v a at Sample St and Test Rd", 1000.0)) == []       # the word alone: a hit, no event
    evs = {ev.category: ev for ev in e.feed(crow("t2", "start e m s for the driver", 1010.0))}
    assert set(evs) == {"mva_injury", "ems_call"}                           # the crash, and EMS asked for
    assert (evs["mva_injury"].mode, evs["mva_injury"].traffic_ids) == ("log", ["t1", "t2"])
    assert {h.rule_id for h in sink.hits} >= {"cpd.mva_word", "cpd.mva_ems"}                # every hit is counted (recall)
    e2, _ = engine()
    e2.feed(crow("t1", "two car m v a at Sample St and Test Rd", 1000.0))
    late = e2.feed(crow("t2", "start e m s for the driver", 1000.0 + 300))                  # outside the window: not tied
    assert [ev.category for ev in late] == ["ems_call"]


def test_a_resolved_call_is_damped():
    assert raised("smoke coming from the roof", CPD, "police")[0] == {("county_police", "fire_report")}
    assert raised("smoke coming from the roof unfounded", CPD, "police")[0] == set()
    assert raised("accident with injuries, refused medical attention", CPD, "police")[0] == set()
    assert raised("second alarm", FG, "fire")[0] == {("fire", "more_units")}
    assert raised("second alarm earlier today, units returning", FG, "fire")[0] == set()


# -- nothing of these packs is pushed, mapped or shown -----------------------------------------------------------------

LIVE_ROWS = [                                              # (source label, talkgroup, group, service, text)
    ("County PD", 363, "County Police", "police", "respond to 100 Example Ave order of gas outside"),
    ("County PD", 363, "County Police", "police", "shots fired Sample St and Test Rd"),
    ("County PD", 363, "County Police", "police", "he's not breathing starting c p r"),
    ("Fire/EMS", 307, "Fire/EMS", "fire", "may day may day firefighter down"),
    ("Fire/EMS", 307, "Fire/EMS", "fire", "command to 60 control transmit a second alarm"),
    ("Fire/EMS", 307, "Fire/EMS", "fire", "unresponsive male c p r in progress at 100 Example Ave"),
    ("Fire/EMS", 307, "Fire/EMS", "fire", "roll over with entrapment Sample St and Test Rd"),
    ("FG1", None, "", "", "all units evacuate the building"),
]


def live_row(i, label, tg, group, service, text, at):
    r = {"id": f"L{i}-t0", "start": at, "end": at + 3, "source": label, "unit": "", "text": text, "status": "done",
         "model": "whisper", "group": group, "service": service}
    if tg is not None:
        r["talkgroup"] = tg
    return r


@pytest.fixture
def live(tmp_path):
    cad = make_cad(tmp_path)
    n = FakeNotifier()
    cad.intel.notifier, cad.intel.locate = n, example_locate
    cad.comms.context_broadcast = True                     # every viewer signed in: the console's card may show intel
    people(cad)
    return cad, n, CadApi(cad)


def feed_live(cad):
    for i, (label, tg, group, service, text) in enumerate(LIVE_ROWS):
        cad.comms.record(live_row(i, label, tg, group, service, text, cad.clock() + i))
        cad.intel.consider(f"L{i}-t0")                     # (Cad._on_text does it for a row of ours; again is nothing new)
    cad.intel.drain()
    cad.intel.tick(cad.clock() + 60)


HOSTILE = {"intel": {"enabled": True, "push": True, "per_hour": 1000, "per_day": 10000, "per_category_hour": 1000,
                     "quiet_from": "", "quiet_to": ""}}


def hostile_settings() -> dict:
    """Everything a production hub could already have set: the kill switch on, the caps wide open, the first pack,
    its categories and its rules all at push."""
    return {**HOSTILE, **everything_at("push", names=("police",))}


def commit(cad, snap: dict) -> None:
    changes = [(sec, k, v) for sec in ("intel", "intel_packs", "intel_rules") for k, v in (snap.get(sec) or {}).items()]
    cad.settings._commit(changes, "test", [("system", "intel/test", None, "test")])
    cad.intel.reload()


def test_live_the_new_packs_log_and_never_push_map_or_show(live):
    cad, n, api = live
    commit(cad, hostile_settings())
    assert isinstance(cad.intel.sink, LiveSink) and cad.intel.policy.cfg["push"] is True
    before = counts(cad)
    logged = []
    cad.intel.log = logged.append
    feed_live(cad)
    evs = cad.intel.events.rows("1 = 1", (), 100)
    got = {(e["pack"], e["category"]) for e in evs}
    assert got >= {("county_police", "hazmat"), ("county_police", "weapons"), ("county_police", "aided"), ("fire", "mayday"),
                   ("fire", "more_units"), ("ems", "aided"), ("ems", "mva_injury"), ("fire", "evacuate")}
    assert {e["pack"] for e in evs} == set(NEW)
    # logged events only: at "log", never pushed, no push counted, nothing held for a digest
    assert all(e["mode"] == "log" and e["pushed_at"] is None and e["suppressed"] == "" for e in evs)
    assert n.sent == [] and cad.intel.policy.sent == [] and cad.intel.policy.held == []
    assert sum("intel: raised event" in line for line in logged) == len(evs)
    # the console's card and the state every client is sent: no item
    assert cad.intel_admin.state() == {"open": []} and cad.state()["intel"] == {"open": []}
    # the map layer's API: no item for anyone signed in; an admin asking for the log rows by name sees them (tuning)
    for who in (CASEY, JORDAN, VAL):
        for query in ("", "mode=map,push", "status=all"):
            code, out = get(api, "/api/cad/intel/events", query, who)
            assert code == 200 and out["events"] == [], (who, query)
    assert get(api, "/api/cad/intel/events", "mode=log", VAL)[1]["events"] == []
    assert len(get(api, "/api/cad/intel/events", "mode=log", CASEY)[1]["events"]) == len(evs)
    # every hit is kept for tuning, and intel wrote no incident, link, incoming notice or alarm
    assert cad.store.one("SELECT COUNT(*) AS n FROM intel_hits")["n"] >= len(LIVE_ROWS)
    assert counts(cad) == before
    # time moving on (expiry, a digest falling due) sends nothing either
    cad.intel.tick(cad.clock() + 10 ** 6)
    assert n.sent == []


@pytest.mark.parametrize("partial", [
    {"intel_packs": {n: {"categories": {}} for n in NEW}},                                   # nothing raised
    "categories", "rules", "categories+rules", "pack", "pack+categories", "pack+rules",
])
def test_no_single_switch_turns_a_new_pack_on(partial):
    """A category's or a rule's mode alone never lifts an event over its pack's "log"; the pack's alone never over
    its categories' and rules'. (The engine takes the lowest of the three.)"""
    full = everything_at("push")
    if isinstance(partial, str):
        snap = {"intel_packs": {n: {} for n in NEW}, "intel_rules": {}}
        if "pack" in partial:
            for n in NEW:
                snap["intel_packs"][n]["mode"] = "push"
        if "categories" in partial:
            for n in NEW:
                snap["intel_packs"][n]["categories"] = full["intel_packs"][n]["categories"]
        if "rules" in partial:
            snap["intel_rules"] = full["intel_rules"]
    else:
        snap = partial
    for text, source, service in (("shots fired", CPD, "police"), ("mayday mayday", FG, "fire"),
                                  ("c p r in progress", FIRE_TG, "fire")):
        e, sink = engine({**HOSTILE, **snap})
        evs = e.feed(crow("t1", text, 1000.0, source, service))
        assert evs and all(ev.mode == "log" for ev in evs), (partial, [ev.mode for ev in evs])
        assert not any(d.would_push for _e, d in sink.pushes)


def test_an_admin_raising_pack_category_and_rule_is_what_turns_one_on():
    e, sink = engine({**HOSTILE, **everything_at("push")}, locate=example_locate)
    [ev] = e.feed(crow("t1", "respond to 100 Example Ave shots fired", 1000.0))
    assert ev.mode == "push" and [d.would_push for _e, d in sink.pushes] == [True]
    e, sink = engine({**HOSTILE, **everything_at("map")})
    [ev] = e.feed(crow("t1", "shots fired", 1000.0))
    assert ev.mode == "map" and not any(d.would_push for _e, d in sink.pushes)


def test_an_override_for_a_rule_or_pack_that_is_not_there_changes_nothing():
    """Settings saved before these packs existed can only name the first pack and its rules."""
    snap = {**hostile_settings(), "intel_rules": {**hostile_settings()["intel_rules"], "local.old": {
        "pack": "police", "category": "weapons", "any": ["shots fired"], "w": 0.9, "mode": "push"}}}
    packs = {p.name: p for p in rules_mod.load_packs(None, snap)}
    for name in NEW:
        assert packs[name].mode == "log" and all(r.mode == "log" for r in packs[name].rules)
        assert not any(r.id.startswith("local.") for r in packs[name].rules)
    assert any(r.id == "local.old" for r in packs["police"].rules)


# -- once raised, a push still carries no name -------------------------------------------------------------------------

NAMED = [
    ("county_police", CPD, "police", "respond to 100 Example Ave caller Jordan Sampleton states shots fired by Robin Testworth"),
    ("ems", FIRE_TG, "fire", "100 Example Ave patient Jordan Sampleton is not breathing, wife Robin Testworth on scene"),
    ("fire", FIRE_TG, "fire", "mayday mayday firefighter Jordan Sampleton down at 100 Example Ave, Robin Testworth with him"),
]


@pytest.mark.parametrize("pack, source, service, text", NAMED)
def test_a_push_from_a_raised_pack_never_carries_a_name_or_the_transcript(pack, source, service, text):
    e, sink = engine({**HOSTILE, **everything_at("push")}, locate=example_locate)
    evs = e.feed(crow("t1", text, 1000.0, source, service, talker="Casey Exampleson", radio_id="7654321", rig="Car 12"))
    msgs = [d.message for ev, d in sink.pushes if d.would_push and ev.pack == pack]
    assert evs and msgs, (pack, sink.pushes)
    p = e.pack(pack)
    for m in msgs:
        blob = json.dumps(m).lower()
        for word in ("jordan", "sampleton", "robin", "testworth", "casey", "exampleson", "7654321", "car 12", "caller", "patient",
                     "wife", "states", "breathing", "with him"):
            assert word not in blob, (word, m)
        assert m["title"].startswith(f"{p.label} · ") and "100 Example Ave" in m["body"]     # the gazetteer's label only
        assert set(m) == {"title", "body", "tags", "priority", "click"}


@pytest.mark.parametrize("pack, source, service, text", NAMED)
def test_a_place_that_reads_as_a_person_or_an_id_is_withheld(pack, source, service, text):
    """The scrub fails closed for these packs as for the first: a label holding the talker, a radio ID or a plate is
    no place, and a composed push holding one is not sent."""
    bad = {"county_police": "Casey Exampleson Ave, Rye", "ems": "100 Example Ave unit 7654321", "fire": "ABC1234 Example Ave"}[pack]

    def locate(_text, _scope):
        return Place("address", 41.0, -73.66, 25.0, bad, "Rye", True, 0.9)
    logged = []
    sink = RecordingSink()
    e = IntelEngine(settings={**HOSTILE, **everything_at("push")}, events=MemoryEvents(), sink=sink, locate=locate,
                    thread=False, clock=Clock(), log=logged.append)
    evs = e.feed(crow("t1", text, 1000.0, source, service, talker="Casey Exampleson", radio_id="7654321"))
    mine = [ev for ev in evs if ev.pack == pack]
    assert mine and all(ev.precision == "none" and ev.label_full == "" for ev in mine)       # never an event's place
    assert "intel: a place withheld by the scrub" in logged
    for ev, d in sink.pushes:
        if d.would_push:
            assert bad.lower() not in json.dumps(d.message).lower() and "location not heard" in d.message["body"]


def test_the_settings_page_lists_four_packs_and_who_may_change_them(live):
    cad, _n, api = live
    page = cad.intel_admin.page(gate=False)
    assert [p["name"] for p in page["packs"]] == ["police", *NEW]
    assert [p["mode"] for p in page["packs"]] == ["push", "log", "log", "log"]
    for p in page["packs"][1:]:
        assert all(c["mode"] == "log" and not c["edited"] for c in p["categories"]) and all(r["mode"] == "log" for r in p["rules"])
        assert p["scope"]["services"] and all(g.endswith(":*") for g in p["scope"]["sources"])    # never a talkgroup number
    post = lambda b, who: call(api, "/api/cad/settings/intel", b, who)      # noqa: E731
    assert post({"action": "pack", "pack": "fire", "mode": "push"}, VAL)[0] == 403           # an admin's switch only
    assert post({"action": "pack", "pack": "fire", "mode": "push"}, CASEY)[0] == 200
    assert cad.intel.pack("fire").mode == "push"
    assert all(c["mode"] == "log" for c in cad.intel.pack("fire").categories.values())       # ... and still only one of three
    assert post({"action": "reset", "what": "pack", "pack": "fire"}, CASEY)[0] == 200 and cad.intel.pack("fire").mode == "log"


def test_the_new_packs_never_ask_the_model():
    """Rules only ("infer": false): with inference on and every row sampled, their rows are never sent to a model;
    the first pack's still are."""
    class FakeInfer:
        asked: dict = {}

        def __init__(self):
            self.calls = []

        def cfg(self):
            return {"infer_low": 0.0, "infer_sample_pct": 100}

        def submit(self, req, job, done):
            self.calls.append(job["pack"])
            return True

    e, _sink = engine()
    e.infer = FakeInfer()
    for i, (text, source, service) in enumerate((("m v a Sample St", CPD, "police"), ("shots fired", CPD, "police"),
                                                 ("mayday mayday", FG, "fire"), ("c p r in progress", FIRE_TG, "fire"))):
        e.feed(crow(f"t{i}", text, 1000.0 + i, source, service))
    assert e.infer.calls == []
    e.feed(crow("p1", "car accident on Sample St", 1000.0, "p25:mrrs:9211", "police"))
    assert e.infer.calls == ["police"]
    for name in NEW:
        assert e.pack(name).src.get("infer") is False


# -- an event's mode is its rules' rung (#535 review) ------------------------------------------------------------------

def test_one_weak_rule_raised_to_push_never_carries_the_strong_ones_with_it():
    """The review's scenario: the pack, the category and only cpd.mva_word at push. "crash, vehicle rollover" hits
    cpd.mva_word (push, 0.35) and cpd.mva_serious (log, 0.7): the event is real, but at log, on the rules at log."""
    snap = {**HOSTILE, "intel_packs": {"county_police": {"mode": "push", "categories": {"mva_injury": {"mode": "push"}}}},
            "intel_rules": {"cpd.mva_word": {"mode": "push"}}}
    e, sink = engine(snap)
    [ev] = e.feed(crow("t1", "crash, vehicle rollover on Sample St", 1000.0))
    assert ev.category == "mva_injury" and ev.what_score >= 0.7 and ev.mode == "log"
    assert not any(d.would_push for _e, d in sink.pushes)
    # the strong rule raised too: now the rules at push alone reach the threshold
    snap["intel_rules"]["cpd.mva_serious"] = {"mode": "push"}
    e, sink = engine(snap)
    [ev] = e.feed(crow("t1", "crash, vehicle rollover on Sample St", 1000.0))
    assert ev.mode == "push" and [d.would_push for _e, d in sink.pushes] == [True]


def test_weak_rules_at_push_that_add_up_to_the_threshold_push_together():
    """Two rules at push that only reach the threshold together (a crash said, then EMS asked for) push; a log cue
    beside a push rule adds to WHAT, never to the rung."""
    snap = {**HOSTILE, "intel_packs": {"county_police": {"mode": "push", "categories": {"mva_injury": {"mode": "push"}}}},
            "intel_rules": {"cpd.mva_word": {"mode": "push"}, "cpd.mva_ems": {"mode": "push"}}}
    e, _sink = engine(snap)
    e.feed(crow("t1", "two car m v a on Sample St", 1000.0))
    evs = {ev.category: ev for ev in e.feed(crow("t2", "start e m s for the driver", 1010.0))}
    assert evs["mva_injury"].mode == "push"
    del snap["intel_rules"]["cpd.mva_ems"]
    e, _sink = engine(snap)
    e.feed(crow("t1", "respond to a two car m v a on Sample St", 1000.0))       # mva_word (push) + a dispatch cue (log)
    evs = {ev.category: ev for ev in e.feed(crow("t2", "start e m s for the driver", 1010.0))}
    assert evs["mva_injury"].mode == "log"


def test_the_police_pack_is_unchanged_with_one_mode_and_fixed_with_mixed_ones():
    """As shipped (every rule at push) the police pack's events are at push as before. With pd.mva_injury lowered
    to log and pd.mva left at push, "MVA with injuries" was pushed on pd.mva_injury's strength; now it is at log."""
    def pd(text, ov):
        e, _ = engine({**HOSTILE, **ov})
        return [ev.mode for ev in e.feed(crow("t1", text, 1000.0, "p25:mrrs:9211", "police"))]
    assert pd("respond for an MVA with injuries at Sample St", {}) == ["push"]
    assert pd("respond for an MVA with injuries at Sample St", {"intel_rules": {"pd.mva_injury": {"mode": "log"}}}) == ["log"]
    assert pd("shots fired", {"intel_rules": {"pd.shots": {"mode": "map"}}}) == ["map"]


def test_the_rung_by_itself():
    from sizeup.intel.engine import rung
    R = lambda mode: rules_mod.Rule("x", 1, "c", 0.5, [], [], mode)      # noqa: E731
    assert rung([(0.7, "t", R("log")), (0.35, "t", R("push"))], [], 0.6) == "log"
    assert rung([(0.7, "t", R("push")), (0.35, "t", R("log"))], [], 0.6) == "push"
    assert rung([(0.4, "t", R("push")), (0.4, "t", R("map")), (0.4, "t", R("log"))], [], 0.6) == "map"
    assert rung([(0.7, "t", R("push")), (0.7, "t", R("log"))], [(-0.3, "t", R("log"))], 0.6) == "log"      # a damper at every rung
    assert rung([], [], 0.6) == "off"


# -- the area: county-wide packs read our district and its neighbours ------------------------------------------------

def place_in(town, label="100 Example Ave", lat=41.0, lon=-73.66):
    return lambda _text, _scope: Place("address", lat, lon, 25.0, f"{label}, {town}", town, True, 0.9)


def test_the_county_wide_packs_have_an_area_and_the_first_pack_none():
    packs = {p.name: p for p in rules_mod.load_packs()}
    assert packs["police"].area == ()
    for name in NEW:
        assert set(packs[name].area) >= {"Port Chester", "Rye Brook", "Rye", "Harrison"}, name
    ov = {"intel_packs": {"ems": {"area": ["port chester", "Rye Brook"]}, "fire": {"area": []}}}
    packs = {p.name: p for p in rules_mod.load_packs(None, ov)}
    assert packs["ems"].area == ("Port Chester", "Rye Brook")              # the county's spelling
    assert packs["fire"].area == rules_mod.load_packs()[3].area            # empty: the file's own
    with pytest.raises(ValueError, match="isn't a town"):
        rules_mod.load_packs(None, {"intel_packs": {"ems": {"area": ["Example Town"]}}})
    assert rules_mod.ruleset_hash(rules_mod.load_packs(None, ov)) != rules_mod.ruleset_hash(rules_mod.load_packs())


@pytest.mark.parametrize("town, held", [("Port Chester", False), ("Rye Brook", False), ("Harrison", False),
                                        ("White Plains", True), ("Yonkers", True)])
def test_an_event_placed_outside_the_area_is_held_at_log(town, held):
    e, sink = engine({**HOSTILE, **everything_at("push")}, locate=place_in(town))
    [ev] = e.feed(crow("t1", "respond to 100 Example Ave shots fired", 1000.0))
    assert ev.town == town and (ev.suppressed == "out_of_area") is held
    assert ev.mode == ("log" if held else "push")
    assert any(d.would_push for _e, d in sink.pushes) is not held


@pytest.mark.parametrize("text, held", [
    ("odor of gas in White Plains", True),
    ("odor of gas at the school in the area of Yonkers", True),
    ("odor of gas in Port Chester, White Plains units responding", False),   # one of ours as the place: not held
    ("odor of gas near Harrison Ave", False),                                 # a street of that name: no town said
    ("odor of gas", False),                                                   # nothing said: not known, not held
    ("odor of gas in West Harrison", False),
    # the review's: a town that isn't where the call is (the hospital, the units coming)
    ("c p r in progress transporting to White Plains", False),
    ("ems en route to New Rochelle for a fall", False),
    ("odor of gas, units from Valhalla responding", False),
    ("Yonkers PD reporting odor of gas", False),
])
def test_with_no_place_only_a_town_said_as_the_place_decides(text, held):
    e, _sink = engine({**HOSTILE, **everything_at("push")})
    evs = e.feed(crow("t1", text, 1000.0))
    assert evs
    for ev in evs:
        assert (ev.suppressed == "out_of_area") is held, (text, ev.category)
        assert ev.mode == ("log" if held else "push")


def test_a_safety_event_with_no_place_is_never_held_for_a_town_said():
    e, _sink = engine({**HOSTILE, **everything_at("push")})
    [ev] = e.feed(crow("t1", "shots fired in White Plains", 1000.0))
    assert ev.category == "weapons" and ev.suppressed == "" and ev.mode == "push"
    e, _sink = engine({**HOSTILE, **everything_at("push")}, locate=place_in("White Plains"))
    [ev] = e.feed(crow("t1", "respond to 100 Example Ave shots fired", 1000.0))
    assert ev.suppressed == "out_of_area"                                     # placed elsewhere: held


def test_the_lookup_is_held_to_the_area_and_runs_only_for_what_could_raise():
    seen = []

    def loc(text, scope):
        seen.append((scope["pack"], scope["only"], tuple(scope["towns"]), text))
        return None
    e, _sink = engine(locate=loc)
    e.feed(crow("t1", "respond to 100 Example Ave for a parking complaint", 1000.0))     # a cue and a damper: no lookup
    e.feed(crow("t2", "m v a at 10 Sample St", 1300.0))                                  # a weak hit alone: no lookup
    assert seen == []
    e.feed(crow("t3", "respond to 100 Example Ave shots fired", 1600.0))
    assert {(pack, only) for pack, only, _t, _x in seen} == {("county_police", True)}
    assert all("Port Chester" in towns and "White Plains" not in towns for _p, _o, towns, _x in seen)
    e2, _ = engine(locate=loc)
    e2.feed(crow("t1", "m v a at 10 Sample St", 1000.0))
    seen.clear()
    e2.feed(crow("t2", "start e m s for the driver at 10 Sample St", 1010.0))            # with the crash: could reach
    assert {s[0] for s in seen} == {"county_police"}
    seen.clear()
    e3, _ = engine(locate=loc)
    e3.feed(crow("p1", "respond to 100 Example Ave smoke coming from the house", 1000.0, "p25:mrrs:9211", "police"))
    assert {(s[0], s[1]) for s in seen} == {("police", False)}                          # the first pack: as before


# -- near_incident: an event at one of our own incidents is that incident ------------------------------------------

def incidents(*rows):
    return lambda _now: [dict(r) for r in rows]


OURS = {"id": 41, "lat": 41.0, "lon": -73.66, "label": "100 Example Ave, Port Chester", "created_at": 900.0, "closed_at": None}


@pytest.mark.parametrize("inc, linked", [
    (OURS, True),                                                             # the same address, open
    ({**OURS, "label": "", "lat": 41.0005, "lon": -73.6605}, True),           # 70 m away
    ({**OURS, "label": "", "lat": 41.01, "lon": -73.67}, False),              # 1.4 km away
    ({**OURS, "closed_at": 950.0}, False),                                    # closed before the event
    ({**OURS, "created_at": 1000.0 - 7 * 3600}, False),                       # opened too long before
    ({**OURS, "created_at": 1000.0 + 1200}, True),                            # opened soon after (the radio heard first)
])
def test_an_event_at_our_incident_is_linked_and_no_new_lead(inc, linked):
    e, sink = engine({**HOSTILE, **everything_at("push")}, locate=place_in("Port Chester"))
    e.incidents = incidents(inc)
    [ev] = e.feed(crow("t1", "respond to 100 Example Ave odor of gas", 1000.0))
    assert ev.category == "hazmat"
    assert ev.near_incident == (41 if linked else None)
    assert ev.suppressed == ("near_incident" if linked else "") and ev.mode == ("log" if linked else "push")


def test_a_safety_event_at_our_incident_is_linked_and_still_raised():
    e, sink = engine({**HOSTILE, **everything_at("push")}, locate=place_in("Port Chester"))
    e.incidents = incidents(OURS)
    [ev] = e.feed(crow("t1", "respond to 100 Example Ave shots fired", 1000.0))
    assert ev.near_incident == 41 and ev.suppressed == "" and ev.mode == "push"
    [ev] = e.feed(crow("f1", "mayday mayday at 100 Example Ave", 1010.0, FG, "fire"))
    assert ev.near_incident == 41 and ev.mode == "push"


def test_an_event_that_reaches_our_incident_later_is_linked_then():
    e, _sink = engine(locate=place_in("Port Chester"))
    e.incidents = incidents()
    [ev] = e.feed(crow("t1", "respond to 100 Example Ave odor of gas", 1000.0))
    assert ev.near_incident is None                                           # first: a lead
    e.incidents = incidents({**OURS, "created_at": 1050.0})
    [ev] = e.feed(crow("t2", "odor of gas getting stronger at 100 Example Ave", 1060.0))
    assert ev.near_incident == 41 and ev.suppressed == "near_incident"


def test_live_our_incidents_are_read_from_the_cad(tmp_path):
    cad = make_cad(tmp_path)
    now = cad.clock()
    with cad.store.tx() as db:
        for status, lat, created, closed, emergency, accepted in (
                ("active", 41.0, now - 600, None, 1, now - 590),           # ours
                ("closed", 41.0, now - 500, now - 300, 1, now - 490),      # ours, closed since
                ("dismissed", 41.0, now - 600, None, 1, None),
                ("queued", 41.0, now - 600, None, 1, None),                # a page nobody took (yet)
                ("active", 41.0, now - 600, None, 0, now - 590),           # a notification or a closure
                ("active", None, now - 600, None, 1, now - 590),           # no place
                ("active", 41.0, now - 7 * 3600, None, 1, now - 7 * 3600)):
            db.execute("INSERT INTO incidents (type, status, source, created_at, closed_at, lat, lon, address, geo_label, "
                       "emergency, accepted_at) VALUES ('FIRE', ?, 'manual', ?, ?, ?, -73.66, '100 Example Ave', "
                       "'100 Example Ave, Port Chester', ?, ?)", (status, created, closed, lat, emergency, accepted))
    got = cad.intel_incidents(now)
    assert [g["closed_at"] is None for g in got] == [False, True]             # newest first: the closed one, the open one
    plan = " ".join(str(tuple(r)) for r in cad.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM incidents WHERE created_at >= ? AND emergency = 1 ORDER BY created_at DESC",
        (now,)).fetchall())
    assert "incidents_created" in plan                                        # an index walk, never the whole table
    assert all(g["label"] == "100 Example Ave, Port Chester" and g["lat"] == 41.0 for g in got)
    assert cad.intel.incidents == cad.intel_incidents


@pytest.mark.parametrize("town, sure, want_town, held", [
    ("Port Chester", True, "Port Chester", False),        # confirmed (a cross street, a read-back): ours
    ("Port Chester", False, "", False),                   # unconfirmed into our town: no place (a street anywhere)
    ("White Plains", False, "White Plains", True),        # unconfirmed into another town: still out of the area
])
def test_a_county_wide_reading_into_our_towns_counts_only_confirmed(town, sure, want_town, held):
    def loc(_text, _scope):
        return Place("address", 41.0, -73.66, 25.0, f"100 Example Ave, {town}", town, sure, 0.7)
    e, _sink = engine(locate=loc)
    [ev] = e.feed(crow("t1", "respond to 100 Example Ave shots fired", 1000.0))
    assert ev.town == want_town and (ev.suppressed == "out_of_area") is held
    e, _sink = engine(locate=loc)                         # the first pack (no area): every reading, as before
    [ev] = e.feed(crow("p1", "respond to 100 Example Ave shots fired", 1000.0, "p25:mrrs:9211", "police"))
    assert ev.town == town


# -- the #539 review's fixes -----------------------------------------------------------------------------------------

AT_OURS = {"id": 41, "lat": 41.001, "lon": -73.66, "label": "", "created_at": 1000.0 - 3600, "closed_at": None}   # ~110 m


@pytest.mark.parametrize("text", ["smoke coming from the roof at 100 Example Ave", "accident with injuries at 100 Example Ave"])
def test_the_police_pack_is_never_held_or_linked_by_our_incidents(text):
    e, sink = engine({**HOSTILE}, locate=place_in("Port Chester"))
    e.incidents = incidents(AT_OURS)
    [ev] = e.feed(crow("p1", text, 1000.0, "p25:mrrs:9211", "police"))
    assert ev.mode == "push" and ev.suppressed == "" and ev.near_incident is None
    assert [d.would_push for _e, d in sink.pushes] == [True]
    e.incidents = incidents({**AT_OURS, "created_at": 1100.0})                 # one of ours opened after the push
    e.feed(crow("p2", text.replace("at", "still at"), 1200.0, "p25:mrrs:9211", "police"))
    [ev] = e.events.open_events()
    assert ev.mode == "push" and ev.suppressed == "" and ev.near_incident is None


def test_a_pushed_event_is_never_lowered_by_a_hold():
    e, sink = engine({**HOSTILE, **everything_at("push")}, locate=place_in("Port Chester"))
    e.incidents = incidents()
    [ev] = e.feed(crow("t1", "respond to 100 Example Ave odor of gas", 1000.0))
    assert ev.mode == "push" and ev.pushed_at is not None
    e.incidents = incidents({**OURS, "created_at": 1050.0})
    e.feed(crow("t2", "odor of gas getting stronger at 100 Example Ave", 1060.0))
    [ev] = e.events.open_events()
    assert ev.near_incident == 41 and ev.mode == "push" and ev.suppressed == ""      # linked, still on the map


def test_a_hold_that_ends_gives_the_rules_mode_back():
    """Placed elsewhere at first (held), then read back at one of our streets: the event is ours, at the mode its
    rules give, not stuck at log."""
    where = {"town": "White Plains", "sure": False}

    def loc(_text, _scope):
        return Place("address", 41.0, -73.66, 25.0, f"100 Example Ave, {where['town']}", where["town"], where["sure"], 0.6)
    e, _sink = engine({**HOSTILE, **everything_at("push")}, locate=loc)
    [ev] = e.feed(crow("t1", "respond to 100 Example Ave odor of gas", 1000.0, talker="Car 12"))
    assert ev.suppressed == "out_of_area" and ev.mode == "log"
    where.update(town="Port Chester", sure=True)
    [ev] = e.feed(crow("t2", "100 Example Ave at Sample St, copy", 1030.0, talker="Car 12"))
    assert ev.town == "Port Chester" and ev.suppressed == "" and ev.mode == "push"


@pytest.mark.parametrize("text, source, service, pack", [
    ("stuck elevator no one inside", CPD, "police", "county_police"),
    ("elevator stuck, no one inside", FIRE_TG, "fire", "ems"),
    ("people trapped no one trapped", FG, "fire", "fire"),
    ("vehicle in the water no one in the water", CPD, "police", "county_police"),
    ("vehicle in the water no one in the water", FIRE_TG, "fire", "ems"),
])
def test_a_not_phrase_holds_on_the_tables_reading_too(text, source, service, pack):
    assert {c for p, c in raised(text, source, service)[0] if p == pack} == set()


def test_a_not_phrase_in_heard_spelling_does_not_compile():
    d = {"name": "t", "normalize": "stt", "categories": {"c": {}},
         "rules": [{"id": "t.a", "category": "c", "any": ["elevator"], "not": ["(no one|nobody) inside"]}]}
    with pytest.raises(ValueError, match="add 'no 1' beside 'no one'"):
        rules_mod.compile_pack(d)
    d["rules"][0]["not"] = ["first alarm"]
    with pytest.raises(ValueError, match="add '1st' beside 'first'"):
        rules_mod.compile_pack(d)
    d["rules"][0]["not"] = ["(no one|no 1|nobody) inside", "1st alarm"]
    rules_mod.compile_pack(d)
    rules_mod.compile_pack({**d, "normalize": "", "rules": [{**d["rules"][0], "not": ["(no one|nobody) inside"]}]})   # no table


def test_a_cues_mode_never_moves_an_event():
    """A cue is evidence beside a category's rule, as before the rung: lowering the dispatch cue never lowers the
    police pack's events, and raising it never lifts the new packs' ones."""
    def pd(ov):
        e, _ = engine({**HOSTILE, **ov})
        return [ev.mode for ev in e.feed(crow("p1", "respond to Sample St, subject is armed", 1000.0, "p25:mrrs:9211", "police"))]
    assert pd({}) == ["push"]
    assert pd({"intel_rules": {"pd.dispatch_cue": {"mode": "map"}}}) == ["push"]
    assert pd({"intel_rules": {"pd.dispatch_cue": {"mode": "log"}, "pd.urgent_cue": {"mode": "log"}}}) == ["push"]
    snap = {**HOSTILE, "intel_packs": {"county_police": {"mode": "push", "categories": {"mva_injury": {"mode": "push"}}}},
            "intel_rules": {"cpd.dispatch_cue": {"mode": "push"}}}
    e, _ = engine(snap)
    [ev] = e.feed(crow("t1", "respond to a rollover on Sample St", 1000.0))
    assert ev.mode == "log"


def test_the_place_word_cache_never_answers_for_another_set_of_towns():
    import random
    from sizeup.geo.gazetteer import Gazetteer
    from sizeup.geo.pagegeo import _word_match, locator
    loc = locator(Gazetteer())
    munis = sorted({p["muni"] for p in loc.places if p["muni"]})
    words = sorted({w for w in loc.place_index if len(w) >= 6})[:40]
    rnd = random.Random(3)
    for _ in range(200):
        towns = set(rnd.sample(munis, rnd.randint(1, min(3, len(munis)))))
        index = loc._index_for(towns)
        w = rnd.choice(words)
        assert set(loc._like(towns, index, w)) == {x for x in index if x != w and _word_match(w, x)}, towns


def test_one_bad_override_never_drops_the_others():
    logged = []
    settings = {"intel_packs": {"ems": {"area": ["Example Town"]}, "police": {"threshold": 0.7}},
                "intel_rules": {"pd.shots": {"mode": "map"}}}
    e = IntelEngine(settings=settings, events=MemoryEvents(), sink=RecordingSink(), thread=False, clock=Clock(),
                    log=logged.append)
    assert e.pack("police").threshold == 0.7 and next(r for r in e.pack("police").rules if r.id == "pd.shots").mode == "map"
    assert e.pack("ems").area == rules_mod.load_packs()[2].area                # the file's area
    assert any("intel_packs/ems not applied" in m for m in logged)


# -- every "re:" expression of every pack runs in linear time on hostile texts (#670's review) ----------------------------

HOSTILE_TEXTS = [
    "dryer" + "'" * 20000 + " x",                    # a run of apostrophes: once both a separator and a word
    "outlet" + "." * 20000 + "x",                    # a run of dots: the ellipsis
    "dryer " + ". " * 10000 + "x",
    "outlet " + "..x " * 5000,
    "outlet" + " " * 20000 + "x",
    "man" + " ," * 10000 + " x",
    "panel " + "a " * 25000,
    "the dryer " + "it's " * 10000,
    "the man " + "a' " * 10000,
    "x" * 50000,
    "the dryer in the ' basement is... " * 1500,   # a 50k-character transcript of near misses
    "!?.'-, " * 8000,
]
HARD_STOP_S = 0.5                                    # a match still running then has hung: the test fails, never hangs


def found_within(src: str, text: str, limit: float) -> float:
    """Phrase(src).found(text): its time, or AssertionError past limit. SIGALRM interrupts `re` (it checks for
    signals while it backtracks); without it (no setitimer, or not the main thread), a child process with a timeout."""
    import signal
    import threading
    if hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread():
        def stop(_sig, _frame):
            raise TimeoutError
        before = signal.signal(signal.SIGALRM, stop)
        signal.setitimer(signal.ITIMER_REAL, limit)
        t0 = time.perf_counter()
        try:
            rules_mod.Phrase(src).found(text, [])
        except TimeoutError:
            raise AssertionError(f"{src[:60]!r} still running after {limit} s on {text[:20]!r}…") from None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, before)
        return time.perf_counter() - t0
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    t0 = time.perf_counter()
    proc = ctx.Process(target=_found, args=(src, text))
    proc.start()
    proc.join(limit + 5)                            # the child's own start-up is not the expression's time
    if proc.is_alive():
        proc.terminate()
        proc.join()
        raise AssertionError(f"{src[:60]!r} still running on {text[:20]!r}…")
    return 0.0 if proc.exitcode == 0 else float("inf")


def _found(src: str, text: str) -> None:
    rules_mod.Phrase(src).found(text, [])


def test_every_re_expression_runs_fast_on_hostile_texts():
    exprs = {p.src for pack in rules_mod.load_packs() for r in pack.rules for p in [*r.any, *r.nots] if p.rx is not None}
    assert len(exprs) >= 4                                         # the police pack's own, at least
    for src in sorted(exprs):
        for text in HOSTILE_TEXTS:
            took = found_within(src, text, HARD_STOP_S)
            assert took < 0.05, f"{src[:60]!r} took {took:.3f} s on {text[:20]!r}…"


def test_a_hung_expression_fails_the_timing_test_instead_of_hanging_it():
    """The pattern #670's review found: "\\W+" beside "[\\w']+" backtracks without bound on a run of apostrophes."""
    bad = r"re:\b(dryer|stove)(\W+(?!(no|not)\b)[\w']+){0,3}\W+(smoking|sparking)\b"
    t0 = time.perf_counter()
    with pytest.raises(AssertionError, match="still running"):
        found_within(bad, "dryer" + "'" * 4000 + " x", 0.2)
    assert time.perf_counter() - t0 < 10


def test_an_expression_reads_at_most_re_text_max_characters():
    p = rules_mod.Phrase("re:smoking")
    assert p.found("x " * 100 + "smoking", [])
    assert not p.found("x" * rules_mod.RE_TEXT_MAX + " smoking", [])


def test_an_expression_is_at_most_re_max_characters():
    assert rules_mod.RE_MAX == 300
    rules_mod.Phrase("re:" + "a" * rules_mod.RE_MAX)
    with pytest.raises(rules_mod.PhraseError, match="1 to 300 characters"):
        rules_mod.Phrase("re:" + "a" * (rules_mod.RE_MAX + 1))
