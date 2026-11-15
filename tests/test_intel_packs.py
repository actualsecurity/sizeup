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
    assert hit("transmit a second alarm") == {"t.raw", "t.fixed"}           # the words as transcribed still match
    assert hit("transmit a 2nd alarm") == {"t.fixed"}
    assert hit("fire alarm") == {"t.not"}                                   # a "not" phrase is tried on both readings
    assert hit("order of gas") == {"t.re"}                                  # an expression too, on the fixed words
    assert n.apply(["order", "of", "gas"]) == ["odor", "of", "gas"] and n.apply(["order", "up"]) is None
    assert n.apply(["second", "second"]) == ["2nd", "2nd"]                  # one pass: what a fix wrote is not fixed again
    d2 = dict(d, normalize="")
    plain = rules_mod.compile_pack(d2)
    said = "transmit a second alarm"                                        # without the table: the words as transcribed only
    assert {r.id for r in plain.rules if r.hit(said, rules_mod.words(said))} == {"t.raw", "t.not"}
    assert rules_mod.ruleset_hash([p]) != rules_mod.ruleset_hash([plain])   # the table is part of what decides a hit


@pytest.mark.parametrize("table, why", [
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
    "ambulance responding to 10 Sample St for a fall", "responding", "m v a Sample St", "patient refusal r m a",
    "scene is safe", "transporting one to the hospital", "structure fire Example Ave", "odor of gas in the basement",
    "requesting an additional engine", "partial roof collapse", "this is a drill c p r in progress", "back in service",
    "you're not responding", "clear of the lift assist", "medic 4 patients are stable",
    "no c o readings, patient feels sick", "holding for police to bring the keys", "going out of service for decon",
]
FIRE = [
    ("may day may day may day", "mayday"),
    ("firefighter down second floor", "mayday"),
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
