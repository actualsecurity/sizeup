"""Who said it and its town (sizeup/intel/engine.py _talkers): the row that first says an event's category is its
stater; an officer's radio is answered by the next dispatch console on the source. Their towns (Settings > Radios
labels, sizeup/intel/talkers.py) give the event's talker town: with no place heard, its coarse place ("<Town>
(talker)", precision town: model.Event.shown), the push's and the Intel page's; a heard place always wins. A radio ID
heard in two towns, or labelled off its talkgroup, is flagged for Health, never pushed. Every radio ID, label and
place here is invented (this repository is public)."""

from __future__ import annotations

import sqlite3
import threading


from sizeup.intel import engine as engine_mod
from sizeup.intel import rules as rules_mod
from sizeup.intel.events import SqlEvents
from sizeup.intel import push as push_mod
from sizeup.intel.model import Event, initials, resolved_town, shown_place, talkers_line, talkers_of
from tests.test_intel import Clock, engine, row
from tests.test_intel_page import T0, listing, save, site  # noqa: F401  (the fixture)

PC, RYE = "Port Chester", "Rye"         # two of the police pack's dispatch towns (data/intel/police.json, public)
OFFICER, BLANK, RYE_DISPATCH, PC_DISPATCH, AWAY, RYE_OFFICER, BLANK2 = "7701", "7702", "7801", "7802", "7703", "7704", "7705"
LABELS = {OFFICER: "Sampletown PD officer", RYE_DISPATCH: "Rye PD dispatch", PC_DISPATCH: "Port Chester PD dispatch",
          AWAY: "Harrison PD officer", RYE_OFFICER: "Rye PD officer"}
AGENCY_TOWNS = {"Sampletown PD": PC}
NO_PLACE = "Car 3, a male with a knife behind the deli"          # a weapons call, no place a gazetteer reads
AT_CORNER = "Car 3, a male with a knife at Sample St and Test Rd"  # example_locate: an intersection in Rye
ANSWER = "copy car 3, en route"                                    # a console's answer: no category words


class FakeRadios:
    def __init__(self, labels=None):
        self.labels = dict(LABELS if labels is None else labels)
        self.rev = 1

    def name(self, system, radio_id):
        return self.labels.get(radio_id, "") if system == "mrrs" else ""

    def rows(self):
        return [{"system": "mrrs", "radio_id": k, "label": v} for k, v in self.labels.items()]

    def ranges(self):
        return []


def labelled(clock=None):
    e, sink = engine(packs=rules_mod.load_packs(overrides={"intel_packs": {"police": {"agency_towns": AGENCY_TOWNS}}}),
                     clock=clock)
    e.talkers.radios = FakeRadios()
    return e, sink


def the_event(e) -> Event:
    [ev] = [x for x in e.events.all() if x.category == "weapons"]
    return ev


# -- stated by, answered by, the town ----------------------------------------------------------------------------------

def test_an_unlabelled_radio_answered_by_a_labelled_console_takes_the_consoles_town():
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=BLANK))
    ev = the_event(e)
    assert (ev.stated_by, ev.answered_by, ev.talker_town) == ("", "", "")      # no answer yet: no town
    e.feed(row("t2", ANSWER, 1006, radio_id=RYE_DISPATCH))
    ev = the_event(e)
    assert (ev.answered_by, ev.answered_town, ev.talker_town) == ("Rye PD dispatch", RYE, RYE)
    assert ev.precision == "none"                                              # the heard place: still none
    assert shown_place(ev) == {"place": "Rye (talker)", "precision": "town", "town": RYE, "basis": "talker"}
    assert ev.lat is None and resolved_town(ev) == RYE


def test_the_answer_comes_first_when_transcribed_out_of_order():
    e, _sink = labelled()
    e.feed(row("t2", ANSWER, 1006, radio_id=RYE_DISPATCH))     # the console's answer read first (FIFO)
    e.feed(row("t1", NO_PLACE, 1000, radio_id=BLANK))
    assert the_event(e).talker_town == RYE


def test_a_late_or_unrelated_console_is_no_answer():
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=BLANK))
    e.feed(row("t2", ANSWER, 1000 + engine_mod.ANSWER_S + 5, radio_id=RYE_DISPATCH))     # too long after
    assert the_event(e).talker_town == ""
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=BLANK))
    e.feed(row("t2", "units respond for a car accident with injuries", 1005, radio_id=RYE_DISPATCH))   # another job
    assert the_event(e).answered_by == "" and the_event(e).talker_town == ""


def test_a_labelled_officer_with_no_place_is_its_town_talker():
    e, sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=OFFICER))
    ev = the_event(e)
    assert (ev.stated_by, ev.stated_town, ev.talker_town) == ("Sampletown PD officer", PC, PC)
    [(_ev, d)] = [x for x in sink.pushes if x[1].would_push]
    assert d.message["title"].endswith(f"· {PC}")
    assert d.message["body"].splitlines()[0] == "Port Chester (talker)"
    assert OFFICER not in d.message["body"]


def test_a_console_stater_is_its_own_town_and_has_no_answer():
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=PC_DISPATCH))
    e.feed(row("t2", ANSWER, 1004, radio_id=RYE_DISPATCH))
    ev = the_event(e)
    assert (ev.stated_by, ev.answered_by, ev.talker_town) == ("Port Chester PD dispatch", "", PC)


def test_a_heard_place_beats_the_talker_town():
    e, sink = labelled()
    e.feed(row("t1", AT_CORNER, 1000, radio_id=OFFICER))       # a PC officer's radio, a corner heard in Rye
    ev = the_event(e)
    assert ev.talker_town == PC and ev.precision == "intersection" and ev.town == RYE
    assert shown_place(ev) == {} and resolved_town(ev) == RYE
    [(_ev, d)] = [x for x in sink.pushes if x[1].would_push]
    assert d.message["title"].endswith(f"· {RYE}") and "(talker)" not in d.message["body"]


def test_a_later_heard_place_replaces_the_talker_town():
    e, sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=OFFICER))
    assert shown_place(the_event(e))["precision"] == "town"
    e.feed(row("t2", AT_CORNER, 1010, radio_id=OFFICER))
    ev = the_event(e)
    assert ev.precision == "intersection" and shown_place(ev) == {} and resolved_town(ev) == RYE
    # the place found after the first push (the read-back): pushed once more, as when no town was shown at all
    assert [d.message["body"].splitlines()[0] for _e, d in sink.pushes if d.would_push] == [
        "Port Chester (talker)", "Sample St & Test Rd"]


def test_stater_and_answerer_in_different_towns_the_console_wins_and_the_id_is_flagged():
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=OFFICER))        # labelled PC
    e.feed(row("t2", ANSWER, 1005, radio_id=RYE_DISPATCH))     # answered by Rye's console
    ev = the_event(e)
    assert (ev.stated_town, ev.answered_town, ev.talker_town) == (PC, RYE, RYE)
    c = e.counts()
    assert c["talkers"]["text"] == "24 h: two towns: 1" and c["talkers"]["level"] == "warn"
    assert OFFICER in c["talkers"]["title"]                    # the ID in the hover (Health: admins)
    assert e.talker_flags() == [{"kind": "talker_split", "id": f"mrrs:{OFFICER}", "towns": [PC, RYE]}]


def test_an_id_heard_in_two_towns_is_flagged_once_and_ages_out():
    clock = Clock(1000.0)
    e, sink = labelled(clock)

    def feed(tid, text, at, radio):
        clock.t = at + 5                                       # the clock moves with the rows
        e.feed(row(tid, text, at, radio_id=radio))
    feed("t1", NO_PLACE, 1000, BLANK)
    feed("t2", ANSWER, 1005, RYE_DISPATCH)
    assert e.counts()["talkers"] is None                       # one town so far: nothing to say
    feed("t3", NO_PLACE, 4000, BLANK)                          # another call, an hour on: answered by PC
    feed("t4", ANSWER, 4004, PC_DISPATCH)
    feed("t5", NO_PLACE, 7000, BLANK)                          # ... and again: the same ID, said once
    feed("t6", ANSWER, 7004, PC_DISPATCH)
    assert e.counts()["talkers"]["split"] == 1
    assert [f["towns"] for f in e.talker_flags()] == [[PC, RYE]]
    assert all("talker" not in (d.suppressed or "") for _e, d in sink.pushes)          # a flag, never a push
    later = 7004 + engine_mod.FLAG_S + 60                      # over a day on, heard in one town only since
    feed("t7", NO_PLACE, later, BLANK)
    feed("t8", ANSWER, later + 4, PC_DISPATCH)
    assert e.talker_flags() == [] and e.counts()["talkers"] is None
    assert e._heard_in[f"mrrs:{BLANK}"].keys() == {PC}


def test_a_label_off_its_talkgroup_is_flagged():
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=AWAY))           # a town the shared talkgroup doesn't serve
    assert the_event(e).talker_town == ""
    assert e.counts()["talkers"]["off_tg"] == 1
    assert e.talker_flags()[0]["kind"] == "talker_off_tg"


def test_no_registry_reads_as_before():
    e, sink = engine()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=OFFICER))
    e.feed(row("t2", ANSWER, 1005, radio_id=RYE_DISPATCH))
    ev = the_event(e)
    assert (ev.stated_by, ev.answered_by, ev.talker_town) == ("", "", "")
    assert [d.message["body"].splitlines()[0] for _e, d in sink.pushes if d.would_push] == ["location not heard"]


# -- the area gate -----------------------------------------------------------------------------------------------------

def test_the_area_gate_reads_the_talker_town_when_nothing_else_says_where():
    e, _sink = labelled()
    pack = next(p for p in e.packs if p.area)
    area = list(pack.area)
    ev = Event(pack=pack.name, category=next(c for c, d in pack.categories.items() if not d.get("safety")),
               source_key="p25:mrrs:9999")
    assert not e._outside(pack, ev)
    ev.talker_town = area[0]
    assert not e._outside(pack, ev)
    ev.talker_town = "Elsewhere Town"
    assert e._outside(pack, ev)
    ev.precision, ev.town = "address", area[0]          # a heard place wins
    assert not e._outside(pack, ev)


# -- shown: the console, More info, the Intel page -----------------------------------------------------------------------

def test_shown_and_the_talkers_line():
    r = {"precision": "none", "talker_town": RYE, "stated_by": "", "stated_town": "", "answered_by": "Rye PD dispatch",
         "answered_town": RYE}
    assert shown_place(r) == {"place": "Rye (talker)", "precision": "town", "town": RYE, "basis": "talker"}
    assert shown_place({**r, "precision": "address"}) == {}
    assert shown_place({**r, "talker_town": ""}) == {}
    t = talkers_of(r)
    assert t["stated_by"] is None and t["answered_by"] == {"label": "Rye PD dispatch", "town": RYE}
    assert t["line"] == "stated by: unlabelled · answered by: Rye PD dispatch (Rye)"
    assert talkers_line("Sampletown PD officer", PC, "", "") == "stated by: Sampletown PD officer (PC)"
    assert talkers_line("", "", "", "") == ""
    assert (initials(PC), initials("Rye Brook"), initials(RYE)) == ("PC", "RB", "Rye")


def test_the_list_api_and_more_info_carry_the_talker_town(site):  # noqa: F811
    cad, _clock = site
    ev = save(cad, stated_by="Sampletown PD officer", stated_town=PC, answered_by="Rye PD dispatch",
              answered_town=RYE, talker_town=RYE)
    heard_place = save(cad, precision="address", label_full="12 Sample Rd, Port Chester", town=PC, lat=41.0,
                       lon=-73.66, talker_town=RYE, subject="police:weapons:y", first_at=T0 + 1)
    d = listing(cad)
    rows = {r["id"]: r for r in d["events"]}
    a, b = rows[ev.id], rows[heard_place.id]
    assert (a["place"], a["precision"], a["town"], a["lat"]) == ("Rye (talker)", "town", RYE, None)
    assert a["talkers"]["line"] == "stated by: Sampletown PD officer (PC) · answered by: Rye PD dispatch (Rye)"
    assert (b["place"], b["precision"], b["town"]) == ("12 Sample Rd, Port Chester", "address", PC)
    shown = cad.intel_admin.shown(cad.intel.events.rows("id = ?", (ev.id,), 1)[0])
    assert (shown["place"], shown["precision"], shown["town"]) == ("Rye (talker)", "town", RYE)
    assert shown["talkers"]["answered_by"] == {"label": "Rye PD dispatch", "town": RYE}
    from sizeup import context_bundle
    body, _rev = context_bundle._intel(cad, ev.id, True, "viewer", T0, {})
    assert body["why"]["talkers"]["line"].startswith("stated by: Sampletown PD officer")
    assert body["place"]["label"] == "Rye (talker)" and body["place"]["point"] is None
    assert body["place"]["empty"] == "town only"


def test_an_older_table_gets_the_talker_columns():
    class Store:
        lock = threading.RLock()

        def __init__(self):
            self.db = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
            self.db.row_factory = sqlite3.Row

        def q(self, sql, args=()):
            return [dict(r) for r in self.db.execute(sql, args)]

        def one(self, sql, args=()):
            r = self.db.execute(sql, args).fetchone()
            return dict(r) if r else None
    st = Store()
    SqlEvents(st)
    for c in engine_mod.TALKER_FIELDS:                  # a table from before them
        st.db.execute(f"ALTER TABLE intel_events DROP COLUMN {c}")
    ev = SqlEvents(st)
    assert set(engine_mod.TALKER_FIELDS) <= {r["name"] for r in st.q("PRAGMA table_info(intel_events)")}
    saved = ev.save(Event(pack="police", category="weapons", subject="s", first_at=1.0, last_at=1.0, until=2.0,
                          talker_town=RYE, answered_by="Rye PD dispatch", answered_town=RYE))
    assert ev.get(saved.id).talker_town == RYE and ev.get(saved.id).answered_by == "Rye PD dispatch"


# -- the exchange: an answer is the stater's own ---------------------------------------------------------------------

def test_a_console_answering_another_radio_is_not_the_staters_answer():
    """A Rye officer states a call with no place; another radio keys up; the PC console answers that one."""
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE.replace("Car 3, ", ""), 1000, radio_id=RYE_OFFICER))
    e.feed(row("t2", "car 5 out with a parked vehicle", 1003, radio_id=BLANK))
    e.feed(row("t3", "copy car 5", 1006, radio_id=PC_DISPATCH))
    ev = the_event(e)
    assert (ev.stated_by, ev.answered_by, ev.talker_town) == ("Rye PD officer", "", RYE)
    assert e.talker_flags() == []                              # never flagged two-town


def test_a_console_naming_another_unit_is_no_answer():
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=RYE_OFFICER))   # car 3
    e.feed(row("t2", "car 7 what's your location", 1004, radio_id=PC_DISPATCH))
    ev = the_event(e)
    assert ev.answered_by == "" and ev.talker_town == RYE


def test_the_right_answer_on_a_busy_talkgroup():
    e, _sink = labelled()
    e.feed(row("t0", "car 9 back in service", 990, radio_id=BLANK2))        # other traffic before
    e.feed(row("t0b", "copy car 9", 994, radio_id=PC_DISPATCH))
    e.feed(row("t1", NO_PLACE, 1000, radio_id=OFFICER))                     # a PC-labelled radio, car 3
    e.feed(row("t2", "copy car 3, en route", 1004, radio_id=RYE_DISPATCH))  # its answer: names car 3 (backed)
    e.feed(row("t3", "car 7 to dispatch", 1008, radio_id=BLANK2))           # other traffic after
    e.feed(row("t4", "go ahead car 7", 1011, radio_id=PC_DISPATCH))
    ev = the_event(e)
    assert (ev.stated_town, ev.answered_by, ev.answered_town, ev.talker_town) == (PC, "Rye PD dispatch", RYE, RYE)


def test_a_labelled_stater_keeps_its_town_when_the_answer_is_adjacency_only():
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=OFFICER))                     # PC-labelled
    e.feed(row("t2", "standing by", 1003, radio_id=OFFICER))                # the stater again
    e.feed(row("t3", "copy", 1006, radio_id=RYE_DISPATCH))                  # no unit, no street, not straight after
    ev = the_event(e)
    assert (ev.answered_by, ev.talker_town) == ("Rye PD dispatch", PC)
    assert e.talker_flags() == []                              # an adjacency-only answer never flags


def test_the_flag_names_the_radio_that_gave_the_label():
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=f"{BLANK},{OFFICER}"))       # the second ID is the labelled one
    e.feed(row("t2", ANSWER, 1004, radio_id=RYE_DISPATCH))
    assert [f["id"] for f in e.talker_flags()] == [f"mrrs:{OFFICER}"]


def test_a_withdrawn_stater_row_is_no_longer_stated_by():
    e, _sink = labelled()
    e.feed(row("t1", NO_PLACE, 1000, radio_id=OFFICER))
    e.feed(row("t2", "copy, male with a knife behind the deli", 1010, radio_id=BLANK))
    assert the_event(e).stated_by == "Sampletown PD officer"
    e.feed(row("t1", "", 1000, radio_id=OFFICER))                           # its text gone (a re-ask)
    ev = the_event(e)
    assert (ev.stated_by, ev.talker_town) == ("", "")


# -- one town (a handed-off call: the console that took it) --------------------------------------------------------------

def test_a_handoff_town_wins_over_the_talker_town_everywhere():
    """A call handed to another town's dispatch (the event's own town with no place heard) is in that town for the
    push's title and body, the coarse place and the area gate alike."""
    e, _sink = labelled()
    pack = next(p for p in e.packs if p.name == "police")
    ev = Event(pack="police", category="weapons", source_key="p25:mrrs:9211", town="Rye Brook", talker_town=PC,
               first_at=1000.0, level="high")
    assert resolved_town(ev) == "Rye Brook"
    assert shown_place(ev)["place"] == "Rye Brook (talker)"
    msg = push_mod.compose(ev, pack.label, pack.category("weapons"))
    assert msg["title"].endswith("· Rye Brook") and msg["body"].splitlines()[0] == "Rye Brook (talker)"
    area_pack = next(p for p in e.packs if p.area)
    gated = Event(pack=area_pack.name, category=next(c for c, d in area_pack.categories.items() if not d.get("safety")),
                  source_key="p25:mrrs:9999", town="Elsewhere Town", talker_town=list(area_pack.area)[0])
    assert e._outside(area_pack, gated)                        # the handoff's town, not the talker town


def test_search_shows_a_town_only_event_as_its_town(site):  # noqa: F811
    cad, _clock = site
    ev = save(cad, talker_town=RYE)
    d = cad.intel_admin.eventlog.search({})
    [r] = [x for x in d["events"] if x["id"] == ev.id]
    assert (r["place"], r["precision"], r["town"]) == ("Rye (talker)", "town", RYE)
