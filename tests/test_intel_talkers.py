"""Who keyed up (sizeup/intel/talkers.py): a row's radio ID through Settings > Radios > Radio IDs (#844) as the intel
engine reads it: a labelled dispatch console's call-out is a dispatch cue (once), the dispatching department's town is
the place lookup's prior on a source several towns share (a preference: a town said still wins), the push says who it
was heard from by the label, and an unlabelled radio reads exactly as before. The lookup is cached per registry
revision. Every radio ID, label, agency and address here is invented (this repository is public)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from sizeup.geo.gazetteer import SCHEMA, Gazetteer
from sizeup.intel import place as P
from sizeup.intel import rules as rules_mod
from sizeup.intel import talkers as T
from sizeup.intel.model import Place
from tests.test_intel import KEY, Clock, engine, row

PC, RYE = "Port Chester", "Rye"         # two of the police pack's dispatch towns (data/intel/police.json, public)
CONSOLE_ID, OFFICER_ID, OTHER_ID, READBACK_ID, BLANK_ID = "7000001", "7000002", "7000003", "7000004", "7000009"
LABELS = {CONSOLE_ID: "Example PD dispatch", OFFICER_ID: "Example PD officer", OTHER_ID: "Sample PD dispatch"}
AGENCY_TOWNS = {"Example PD": RYE, "Sample PD": PC}
# another of the pack's sources, one with no towns of its own (read from the pack: no talkgroup number written here)
POLICE = next(p for p in rules_mod.load_packs() if p.name == "police")
OTHER_KEY = next(k for k in POLICE.scope["sources"] if k != KEY and "*" not in k and not POLICE.towns_for(k))
OTHER_TG = int(OTHER_KEY.rsplit(":", 1)[-1])
PUNCH = "Car 3, a male was punched outside the deli on Sample Street."          # an assault, no cue words
PUNCH_CUE = "Car 3 can you respond, a male was punched outside the deli on Sample Street."
KNIFE = "Car 3, check 12 Sample Rd for a male with a knife"                      # a weapons call: a push alone


class FakeRadios:
    """The registry's name() and rev (sizeup/cad/radios.py Registry), counting its lookups."""

    def __init__(self, labels=None):
        self.labels = dict(LABELS if labels is None else labels)
        self.rev = 1
        self.calls = 0

    def name(self, system, radio_id):
        self.calls += 1
        return self.labels.get(radio_id, "") if system == "mrrs" else ""

    def rows(self):
        return [{"system": "mrrs", "radio_id": k, "label": v} for k, v in self.labels.items()]

    def ranges(self):
        return []


def police(agency_towns=AGENCY_TOWNS, console_cue=None):
    po = {**({"agency_towns": agency_towns} if agency_towns else {}),
          **({"console_cue": console_cue} if console_cue else {})}
    return rules_mod.load_packs(overrides={"intel_packs": {"police": po}} if po else {})


def labelled(radios=None, locate=None, agency_towns=AGENCY_TOWNS, console_cue=None, clock=None):
    kw = {**({"locate": locate} if locate is not None else {}), **({"clock": clock} if clock is not None else {})}
    e, sink = engine(packs=police(agency_towns, console_cue), **kw)
    e.talkers.radios = radios if radios is not None else FakeRadios()
    return e, sink


def pd_rules(ev) -> list[str]:
    return [r[0] for r in ev.rules]


# -- the label -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("label, agency, role, console", [
    ("Example PD dispatch", "Example PD", "dispatch", True),
    ("Example PD officer", "Example PD", "officer", False),
    ("Sample PD", "Sample PD", "", False),
    ("XPD console 2", "XPD", "console 2", True),
    ("Example Animal Control", "Example Animal", "Control", False),      # another word's "control"
    ("Traffic Control", "Traffic", "Control", False),
    ("XPD HQ Portable 2", "XPD", "HQ Portable 2", False),                 # a radio in the field, "HQ" alone
    ("Desk Sgt", "", "Desk Sgt", False),                                  # "desk" alone
    ("XPD dispatch portable", "XPD", "dispatch portable", False),
    ("XPD officer dispatch", "XPD", "officer dispatch", False),            # never past the role's first word
    ("XPD dispatch desk", "XPD", "dispatch desk", True),
])
def test_a_label_is_agency_role_and_whether_it_is_a_console(label, agency, role, console):
    t = T.parse(label)
    assert (t.label, t.agency, t.role, t.console) == (label, agency, role, console)


def test_the_town_from_settings_else_the_source_towns_by_name_or_initials():
    towns = ["Alpha Town", "Beta", "Gamma Village"]
    assert T.town_of(T.parse("Example PD dispatch"), towns, {"Example PD": "Beta"}) == "Beta"
    assert T.town_of(T.parse("Beta PD officer"), towns) == "Beta"
    assert T.town_of(T.parse("AT PD dispatch"), towns) == "Alpha Town"
    assert T.town_of(T.parse("GVPD officer"), towns) == "Gamma Village"
    assert T.town_of(T.parse("Example PD dispatch"), towns) == ""           # nothing says which
    assert T.town_of(T.parse("Example PD"), towns, {"Example PD": "Delta"}) == ""      # not one of the source's towns
    assert T.town_of(T.parse("AV PD"), ["Alpha Village", "Avon Valley"]) == ""         # two towns' initials: neither


def test_the_settings_agency_towns_are_checked_town_names():
    assert next(p for p in police() if p.name == "police").agency_towns == AGENCY_TOWNS
    with pytest.raises(ValueError):
        rules_mod.load_packs(overrides={"intel_packs": {"police": {"agency_towns": {"Example PD": "Nowhere Town"}}}})


def test_the_lookup_is_cached_per_registry_revision():
    radios = FakeRadios()
    t = T.Talkers(radios)
    r = {"system": "mrrs", "radio_id": CONSOLE_ID}
    assert t.of(r).label == "Example PD dispatch" and t.of(r).console
    blank = {"system": "mrrs", "radio_id": BLANK_ID}
    assert t.of(blank) is None and t.of(blank) is None
    assert radios.calls == 2 and t.asked == 2
    radios.labels[CONSOLE_ID] = "Example PD desk"
    assert t.of(r).label == "Example PD dispatch"        # the same revision: the cache
    radios.rev += 1                                       # an edit in Settings > Radios
    assert t.of(r).label == "Example PD desk" and radios.calls == 3


def test_only_another_systems_radios_are_named():
    t = T.Talkers(FakeRadios())
    assert t.of({"system": "westchester", "radio_id": CONSOLE_ID}) is None
    assert t.of({"system": "mrrs", "radio_id": ""}) is None
    assert t.of({"system": "mrrs", "radio_id": f"{BLANK_ID},{OFFICER_ID}"}).label == "Example PD officer"
    assert T.Talkers(None).of({"system": "mrrs", "radio_id": CONSOLE_ID}) is None


def test_the_engine_asks_once_per_radio_not_per_row():
    radios = FakeRadios()
    e, _sink = labelled(radios)
    for k in range(5):
        e.feed(row(f"t{k}", PUNCH, 1000 + k, radio_id=CONSOLE_ID))
    assert radios.calls == 1


# -- the dispatch cue ------------------------------------------------------------------------------------------------

def test_the_console_cue_is_shadow_by_default_recorded_never_scored():
    e, sink = labelled()
    assert e.feed(row("t1", PUNCH, 1000, radio_id=CONSOLE_ID)) == []
    shadow = [(h.rule_id, h.weight) for h in sink.hits if h.rule_id.endswith(".console")]
    assert shadow == [("pd.dispatch_cue.console", 0.0)]
    assert next(p for p in police() if p.name == "police").console_cue == "shadow"
    e, sink = labelled(console_cue="off")
    assert e.feed(row("t1", PUNCH, 1000, radio_id=CONSOLE_ID)) == []
    assert not [h for h in sink.hits if h.rule_id.endswith(".console")]
    with pytest.raises(ValueError):
        police(console_cue="loud")


def test_a_labelled_console_is_a_dispatch_cue_without_the_words():
    e, sink = engine()                                               # no registry: as before, under the threshold
    assert e.feed(row("t1", PUNCH, 1000, radio_id=CONSOLE_ID)) == []
    e, sink = labelled(console_cue="on")
    [ev] = e.feed(row("t1", PUNCH, 1000, radio_id=CONSOLE_ID))
    assert ev.category == "assault" and pd_rules(ev).count("pd.dispatch_cue") == 1
    [cue] = [r for r in ev.rules if r[0] == "pd.dispatch_cue"]
    assert cue[2] == 0.2 and ev.what_score == 0.6


def test_the_console_cue_is_never_added_twice():
    e, _sink = engine()
    [plain] = e.feed(row("t1", PUNCH_CUE, 1000))
    for mode in ("on", "shadow"):
        e, sink = labelled(console_cue=mode)
        [ev] = e.feed(row("t1", PUNCH_CUE, 1000, radio_id=CONSOLE_ID))
        assert pd_rules(ev).count("pd.dispatch_cue") == 1 and ev.what_score == plain.what_score
        assert not [h for h in sink.hits if h.rule_id.endswith(".console")]      # nothing it would have added


def test_an_officer_or_an_unlabelled_radio_gets_no_cue():
    e, _sink = labelled(console_cue="on")
    assert e.feed(row("t1", PUNCH, 1000, radio_id=OFFICER_ID)) == []
    assert e.feed(row("t2", PUNCH, 1000, radio_id=BLANK_ID, source=OTHER_KEY, talkgroup=OTHER_TG)) == []


def test_a_console_row_with_no_category_words_has_no_hits():
    e, sink = labelled(console_cue="on")
    assert e.feed(row("t1", "Car 3, call the desk when you clear.", 1000, radio_id=CONSOLE_ID)) == []
    assert sink.hits == []


def test_a_console_is_the_dispatcher_never_a_tie_by_talker_alone():
    e, _sink = labelled()
    e.feed(row("t1", PUNCH, 1000, radio_id=CONSOLE_ID))
    [entry] = e.windows[("police", KEY)].entries.values()
    assert entry.dispatcher and entry.label == "Example PD dispatch" and entry.town == RYE


# -- the push says who ------------------------------------------------------------------------------------------------

def test_the_push_says_the_console_label_and_never_the_radio_id():
    e, sink = labelled()
    e.feed(row("t1", KNIFE, 1000, radio_id=CONSOLE_ID))
    [(_ev, d)] = [x for x in sink.pushes if x[1].would_push]
    assert d.message["body"].splitlines()[-1].endswith("· Example PD dispatch")
    assert CONSOLE_ID not in d.message["body"] and CONSOLE_ID not in d.message["title"]


def test_an_unlabelled_push_reads_as_before():
    e, sink = engine()
    e.feed(row("t1", PUNCH_CUE, 1000, radio_id=BLANK_ID))
    e2, sink2 = labelled()
    e2.feed(row("t1", PUNCH_CUE, 1000, radio_id=BLANK_ID))
    [(_a, d1)] = sink.pushes
    [(_b, d2)] = sink2.pushes
    assert d1.message == d2.message and BLANK_ID not in d2.message["body"]


def test_with_the_pii_scrub_on_the_label_is_left_out():
    e2, sink2 = engine(settings={"intel": {"push_pii": False}}, packs=police())
    e2.talkers.radios = FakeRadios()
    e2.feed(row("t1", KNIFE, 1000, radio_id=CONSOLE_ID))
    [(_ev, d)] = sink2.pushes
    assert d.would_push and "Example PD" not in d.message["body"]


# -- the town prior --------------------------------------------------------------------------------------------------

def spy_locate(seen):
    def locate(text, scope):
        seen.append(dict(scope))
        return None
    return locate


def test_the_dispatching_departments_town_is_the_prior():
    seen: list = []
    e, _sink = labelled(locate=spy_locate(seen))
    e.feed(row("t1", "Car 3 respond to 12 Sample Street for a male with a knife", 1000, radio_id=CONSOLE_ID))
    assert seen[-1]["prefer"] == [RYE]
    seen.clear()                                    # the officer's read-back: the window's console still says which
    e.feed(row("t2", "copy 12 Sample Street", 1010, radio_id=READBACK_ID))
    assert seen and all(s.get("prefer") == [RYE] for s in seen)


def test_the_row_talkers_town_when_no_console_says_one():
    seen: list = []
    e, _sink = labelled(locate=spy_locate(seen))
    e.feed(row("t1", "male with a knife at 12 Sample Street", 1000, radio_id=OFFICER_ID))
    assert seen and seen[-1]["prefer"] == [RYE]


def test_no_label_no_prior():
    seen: list = []
    e, _sink = labelled(locate=spy_locate(seen))
    e.feed(row("t1", "Car 3 respond to 12 Sample Street for a male with a knife", 1000, radio_id=BLANK_ID))
    assert seen and all("prefer" not in s for s in seen)
    seen.clear()
    e, _sink = engine(locate=spy_locate(seen))
    e.feed(row("t1", "Car 3 respond to 12 Sample Street for a male with a knife", 1000, radio_id=CONSOLE_ID))
    assert seen and all("prefer" not in s for s in seen)


def test_a_source_of_one_town_has_no_prior():
    seen: list = []
    e, _sink = labelled(locate=spy_locate(seen))
    e.feed(row("t1", "Car 3 respond to 12 Sample Street for a male with a knife", 1000, radio_id=CONSOLE_ID,
               source=OTHER_KEY, talkgroup=OTHER_TG))
    assert seen and all("prefer" not in s for s in seen)


@pytest.fixture(scope="module")
def gaz(tmp_path_factory):
    """Sample Rd in two towns, the same numbers in each, and Cross St meeting it at number 12 in both (invented)."""
    path = tmp_path_factory.mktemp("gaz") / "two-towns.sqlite"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO meta VALUES ('sources', '[]')")
    db.executemany("INSERT INTO streets VALUES (?,?,?)", [(1, "sample rd", "Sample Rd"), (2, "cross st", "Cross St")])
    addrs = []
    for town, lat in ((PC, 41.00), (RYE, 40.97)):
        for k, n in enumerate(range(2, 40, 2)):
            addrs.append((1, n, "", town, lat + k * 0.0001, -73.68, 1))
    db.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?)", addrs)
    db.executemany("INSERT INTO lines VALUES (?,?,?)",
                   [(1, PC, json.dumps([[-73.68, 41.00], [-73.68, 41.01]])),
                    (1, RYE, json.dumps([[-73.68, 40.97], [-73.68, 40.98]])),
                    (2, PC, json.dumps([[-73.69, 41.0005], [-73.67, 41.0005]])),         # Cross St meets it at #12
                    (2, RYE, json.dumps([[-73.69, 40.9705], [-73.67, 40.9705]]))])
    db.executemany("INSERT INTO intersections VALUES (?,?,?,?,?)",
                   [(1, 2, 41.0005, -73.68, PC), (1, 2, 40.9705, -73.68, RYE)])
    db.commit()
    db.close()
    return Gazetteer(path)


SCOPE = {"system": "mrrs", "service": "police", "pack": "police", "towns": [PC, RYE, "Rye Brook"]}


@pytest.mark.parametrize("town", [PC, RYE])
def test_the_prior_picks_its_town_for_a_street_in_both(gaz, town):
    p = P.Locator(gaz)("respond to 12 Sample Rd for a male with a knife", dict(SCOPE, prefer=[town]))
    assert p is not None and p.label.startswith("12 Sample Rd") and p.town == town


@pytest.mark.parametrize("prior, said", [(PC, RYE), (RYE, PC)])
def test_a_town_said_beats_the_prior(gaz, prior, said):
    p = P.Locator(gaz)(f"respond to 12 Sample Rd in {said} for a male with a knife", dict(SCOPE, prefer=[prior]))
    assert p is not None and p.town == said


def test_the_prior_through_the_engine(gaz):
    loc = P.Locator(gaz)
    for rid, town in ((CONSOLE_ID, RYE), (OTHER_ID, PC)):
        e, _sink = labelled(locate=loc)
        [ev] = e.feed(row("t1", KNIFE, 1000, radio_id=rid))
        assert ev.category == "weapons" and ev.town == town and not ev.confirmed


@pytest.mark.parametrize("text, kept", [
    ("respond to 12 Sample Rd", True),
    ("Port Chester PD car 3 respond to 12 Sample Rd", True),        # who, not where
    ("respond to 12 Sample Rd, Rye Brook", False),
    ("respond to 12 Sample Rd in Rye", False),
])
def test_the_prior_is_dropped_when_the_words_name_a_town(text, kept):
    assert P.prior_towns(text, SCOPE["towns"], [PC]) == ([PC] if kept else [])
    assert P.prior_towns(text, [PC], [PC]) == []                     # one town: nothing to prefer


def test_a_callsign_town_still_comes_first(gaz):
    hq = P.hq_towns
    try:
        P.hq_towns = lambda text, system, service: [PC]
        p = P.Locator(gaz)("respond to 12 Sample Rd", dict(SCOPE, prefer=[RYE]))
    finally:
        P.hq_towns = hq
    assert p.town == PC


def test_without_a_prior_the_locator_reads_as_before(gaz):
    a = P.Locator(gaz)("respond to 12 Sample Rd", SCOPE)
    b = P.Locator(gaz)("respond to 12 Sample Rd", dict(SCOPE, prefer=[]))
    assert (a.label, a.town, a.where_score) == (b.label, b.town, b.where_score)


def test_a_stand_in_place_keeps_its_town():
    e, _sink = labelled(locate=lambda text, scope: Place("address", 41.0, -73.66, 25.0, "12 Sample Rd, Port Chester",
                                                          PC, True, 0.9))
    [ev] = e.feed(row("t1", "Car 3, check 12 Sample Rd for a male with a knife", 1000, radio_id=CONSOLE_ID))
    assert ev.town == PC                             # the prior is the locator's, never a rewrite of its answer


# -- the prior never confirms ------------------------------------------------------------------------------------------

CORNER = "respond to 12 Sample Rd at Cross St for a male with a knife"


@pytest.mark.parametrize("prior", [None, PC, RYE])
def test_the_prior_never_confirms_a_corner_in_two_towns(gaz, prior):
    scope = dict(SCOPE, prefer=[prior]) if prior else SCOPE
    p = P.Locator(gaz)(CORNER, scope)
    assert p is not None and not p.confirmed and p.radius_m >= 150
    if prior:
        other = RYE if prior == PC else PC
        assert p.town == prior and any(a.get("town") == other for a in p.alts)


@pytest.mark.parametrize("text", [CORNER, "corner of Sample Rd and Cross St, a male with a knife"])
def test_a_spoken_corner_in_two_towns_is_never_sure_whoever_says_it(gaz, text):
    q = P.Locator(gaz)._spoken_cross(text, SCOPE["towns"], [], [RYE])
    assert q is not None and q.town == RYE and not q.confirmed
    assert any(a["town"] == PC for a in q.alts)


def test_the_prior_never_confirms_through_the_engine(gaz):
    e, _sink = labelled(locate=P.Locator(gaz))
    [ev] = e.feed(row("t1", "Car 3, check 12 Sample Rd at Cross St for a male with a knife", 1000, radio_id=CONSOLE_ID))
    assert ev.category == "weapons" and ev.town == RYE and not ev.confirmed


# -- which talker's town -----------------------------------------------------------------------------------------------

def test_the_rows_own_department_beats_an_unrelated_console():
    seen: list = []
    e, _sink = labelled(locate=spy_locate(seen))
    e.feed(row("t1", "Car 5, a male with a knife at 40 Example Ave", 1000, radio_id=CONSOLE_ID))     # the Rye console
    seen.clear()
    radios = e.talkers.radios
    radios.labels["7000005"] = "Sample PD officer"                                                 # a PC officer
    radios.rev += 1
    e.feed(row("t2", "male with a knife at 12 Sample Street", 1030, radio_id="7000005"))
    assert seen and all(s.get("prefer") == [PC] for s in seen)


def test_an_unlabelled_radio_never_inherits_an_unrelated_console():
    seen: list = []
    e, _sink = labelled(locate=spy_locate(seen))
    e.feed(row("t1", "Car 5, a male with a knife at 40 Example Ave", 1000, radio_id=CONSOLE_ID))
    seen.clear()
    e.feed(row("t2", "Car 8, a male was punched at 12 Sample Street, can you respond", 1030, radio_id=BLANK_ID))
    assert seen and all("prefer" not in s for s in seen)        # another street, unit and category: another job


def test_an_unlabelled_radio_takes_its_jobs_console_town():
    seen: list = []
    e, _sink = labelled(locate=spy_locate(seen))
    e.feed(row("t1", "Car 5, a male with a knife at 40 Example Ave", 1000, radio_id=CONSOLE_ID))
    seen.clear()
    e.feed(row("t2", "car 5 on scene at 40 Example Ave, male with a knife", 1030, radio_id=BLANK_ID))
    assert seen and all(s.get("prefer") == [RYE] for s in seen)


def test_consoles_that_disagree_give_no_prior():
    seen: list = []
    e, _sink = labelled(locate=spy_locate(seen))
    e.feed(row("t1", "Car 5, a male with a knife at 40 Example Ave", 1000, radio_id=CONSOLE_ID))   # Rye
    e.feed(row("t2", "Car 5, a male with a knife at 40 Example Ave", 1010, radio_id=OTHER_ID))     # PC
    seen.clear()
    e.feed(row("t3", "car 5 on scene at 40 Example Ave, male with a knife", 1030, radio_id=BLANK_ID))
    assert seen and all("prefer" not in s for s in seen)


@pytest.mark.parametrize("text, streets", [
    ("check 12 Sample Rd and Cross St", {"sample", "cross"}),
    ("on my way to the 40 block of Example Avenue", {"example"}),
    ("I'll take place of car 3 on the way", set()),
])
def test_the_streets_a_transmission_names(text, streets):
    from sizeup.intel.engine import street_names
    assert street_names(text) == streets


# -- Settings shows the guesses; the cache -----------------------------------------------------------------------------

def test_settings_lists_each_agencys_town_and_how():
    from sizeup.intel.admin import IntelAdmin
    e, _sink = labelled(agency_towns={"Example PD": RYE})
    e.talkers.radios.labels["7000006"] = "Port Chester PD officer"
    e.talkers.radios.labels["7000007"] = "Unknown Agency dispatch"
    pack = next(p for p in e.packs if p.name == "police")
    got = {(g["agency"], g["town"], g["how"]) for g in IntelAdmin(None, e)._agency_guesses(pack)}
    assert got == {("Example PD", RYE, "settings"), ("Sample PD", "", ""), ("Port Chester PD", PC, "name"),
                   ("Unknown Agency", "", "")}


def test_an_answer_from_before_an_edit_is_never_cached():
    radios = FakeRadios()
    t = T.Talkers(radios)
    real = radios.name

    def racing(system, radio_id):
        out = real(system, radio_id)
        radios.rev += 1                 # an edit lands while the old answer is in flight
        return out

    radios.name = racing
    r = {"system": "mrrs", "radio_id": CONSOLE_ID}
    t.of(r)
    radios.name = real
    radios.labels[CONSOLE_ID] = "Example PD desk"
    assert t.of(r).label == "Example PD desk"


# -- a tie by talker never goes through a dispatch console's radio ID (a multi-key-up row "a, b") -----------------------

SAMPLE_CTRL, OFFICER_A, OFFICER_B = "7701", "7702", "7703"
TOWNS_LABELS = {OFFICER_A: "Sampletown PD officer", OFFICER_B: "Othertown PD officer"}


@pytest.mark.parametrize("labels", [
    {SAMPLE_CTRL: "Sampletown PD ctrl", **TOWNS_LABELS},             # a labelled console (the registry says so)
    TOWNS_LABELS,                                                     # a console nothing labels
])
def test_two_officers_answered_by_one_console_never_tie(labels):
    t = 5000.0
    e, _sink = labelled(FakeRadios(labels), clock=Clock(t + 16))
    [ev] = e.feed(row("k", "respond for a male with a knife", t, radio_id=f"{OFFICER_A}, {SAMPLE_CTRL}"))
    e.feed(row("r", "copy, 100 Example Ave", t + 14, radio_id=f"{OFFICER_B}, {SAMPLE_CTRL}"))
    got = e.events.get(ev.id)
    assert got.traffic_ids == ["k"] and got.precision == "none"


def test_an_officers_own_read_back_still_ties_past_the_console():
    t = 5000.0
    e, _sink = labelled(FakeRadios({SAMPLE_CTRL: "Sampletown PD ctrl", **TOWNS_LABELS}), clock=Clock(t + 16))
    [ev] = e.feed(row("k", "respond for a male with a knife", t, radio_id=f"{OFFICER_A}, {SAMPLE_CTRL}"))
    [up] = e.feed(row("r", "copy, 100 Example Ave", t + 14, radio_id=OFFICER_A))
    assert up.id == ev.id and up.traffic_ids == ["k", "r"]
