"""Radio pre-alerts (sizeup/intel, #411): rule packs, the window, events, the policy, the push and its scrub, and the
invariants (no incident / link / incoming / alarm writes; police "266" never Rye Brook FD's; context rows never reach
the fire consumers; pushes from the leader only; idempotent per traffic id; replay writes nothing).

Synthetic only (this repository is public): invented streets ("Example Ave", "Sample St"), made-up radio text, fake
radio IDs and talkers, a fake notifier. No real transcripts, addresses or topics."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from sizeup import notify, sourcekeys
from sizeup.cad.api import CadApi, needs
from sizeup.cloud.access import Identity
from sizeup.intel import place as place_mod
from sizeup.intel import push as push_mod
from sizeup.intel import rules as rules_mod
from sizeup.intel.engine import IntelEngine, LiveSink, RecordingSink
from sizeup.intel.events import MemoryEvents, SqlEvents
from sizeup.intel.model import Event, Place
from sizeup.intel.policy import Policy
from tests.test_cad import make_cad
from tests.test_roles import COREY, JULIAN, VAL, call, people

KEY = "p25:mrrs:9211"                 # the police dispatch source key, as the pack's scope names it


class Clock:
    def __init__(self, t=1050.0):
        self.t = t

    def __call__(self):
        return self.t


def example_locate(text, scope):
    """A stand-in gazetteer: "Example Ave" (confirmed with its number), "Sample St and Test Rd" (an intersection)."""
    low = text.lower()
    if "100 example" in low:                  # "maybe": the words don't confirm it, unless it is read back
        sure = "maybe" not in low or low.count("100 example") > 1
        return Place("address", 41.0, -73.66, 25.0, "100 Example Ave, Port Chester", "Port Chester", sure,
                     0.8 if sure else 0.5)
    if "sample st and test rd" in low:
        return Place("intersection", 41.01, -73.67, 40.0, "Sample St & Test Rd, Rye", "Rye", True, 0.9)
    return None


def row(tid, text, at, **kw):
    r = {"id": tid, "start": at, "end": at + 4, "source": KEY, "system": "mrrs", "service": "police", "dept": "",
         "tier": "alert", "talkgroup": 9211, "channel": "PD Dispatch", "talker": "", "rig": "", "radio_id": "",
         "text": text, "alt_text": "", "model": "whisper"}
    r.update(kw)
    return r


def engine(settings=None, clock=None, locate=example_locate, packs=None):
    sink = RecordingSink()
    e = IntelEngine(packs=packs, settings=settings or {}, events=MemoryEvents(), sink=sink, locate=locate,
                    thread=False, clock=clock or Clock())
    return e, sink


# -- the phrase grammar ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase, text, hit", [
    ("smoke ... (from|coming|showing)", "Caller reports smoke coming out the windows", True),
    ("smoke ... (from|coming|showing)", "there is no smoke coming from it", True),          # "no" before: the rule's not
    ("smoke ... (from|coming|showing)", "smoke but not coming from the house", False),     # a negator inside the gap
    ("smoke ... (from|coming|showing)", "smoke a b c d e f g coming", False),              # gap longer than 6
    ("(car|house) fire", "possible house fire on Example Ave", True),
    ("(car|house) fire", "house is on fire", False),
    ("(fire alarm|co alarm) ... sounding", "CO alarm sounding at the school", True),
    ("re:10-\\d\\d", "advise 10-52 en route", True),
    ("shots fired", "Shots fired!", True),
])
def test_phrases_match_words_in_order_with_gaps_and_alternatives(phrase, text, hit):
    p = rules_mod.Phrase(phrase)
    assert p.found(text, rules_mod.words(text)) is hit


@pytest.mark.parametrize("bad", ["", "... smoke", "smoke ...", "smoke ... ... fire", "(a|) b", "(open", "re:(", "x" * 200])
def test_a_bad_phrase_says_why(bad):
    with pytest.raises(rules_mod.PhraseError):
        rules_mod.Phrase(bad)


def test_packs_load_pure_with_overrides_and_the_hash_follows_them():
    base = rules_mod.load_packs()
    assert [p.name for p in base] == ["police"]
    pol = base[0]
    assert pol.accept is False and all(c["mode"] == "push" for c in pol.categories.values())    # user: push from day one
    h0 = rules_mod.ruleset_hash(base)
    ov = {"intel_packs": {"police": {"threshold": 0.7, "categories": {"alarm": {"mode": "shadow"}}}},
          "intel_rules": {"pd.smoke_seen": {"w": 0.9}}}
    p2 = rules_mod.load_packs(None, ov)[0]
    assert p2.threshold == 0.7 and p2.categories["alarm"]["mode"] == "shadow"
    assert next(r for r in p2.rules if r.id == "pd.smoke_seen").w == 0.9
    assert rules_mod.ruleset_hash([p2]) != h0 and rules_mod.ruleset_hash(rules_mod.load_packs()) == h0
    with pytest.raises(ValueError, match="mode"):
        rules_mod.load_packs(None, {"intel_rules": {"pd.smoke_seen": {"mode": "loud"}}})
    d = json.loads((rules_mod.DATA / "police.json").read_text())
    d["rules"][0]["category"] = "nope"
    with pytest.raises(ValueError, match="category"):
        rules_mod.load_packs([d])


def test_the_police_pack_has_no_real_strings():
    raw = (rules_mod.DATA / "police.json").read_text().lower()
    for s in ("ntfy", "pcfd-", "officer ", "@"):
        assert s not in raw


# -- what raises and what doesn't (TRIGGER / QUIET) ------------------------------------------------------------------

TRIGGER = [
    ("respond to 100 Example Ave report of smoke coming from the second floor", "fire_report"),
    ("caller states shots fired in the area of Sample St and Test Rd", "weapons"),
    ("respond for an MVA with injuries at Sample St and Test Rd", "mva_injury"),
    ("units be advised barricaded male inside 100 Example Ave", "barricade"),
    ("unresponsive male in the lobby, respond to 100 Example Ave", "aided"),
    ("strong odor of gas reported in the basement", "hazmat"),
    ("vehicle pursuit northbound, failed to stop", "pursuit"),
]
QUIET = [
    "car 3 traffic stop on Example Ave, plate to follow",
    "copy, 10-4, back in service",
    "MVA, property damage only, no injuries",
    "the smoke detector is chirping, battery issue",
    "parking complaint at the lot",
    "respond to 100 Example Ave",                 # a dispatch cue alone is never an event
]


@pytest.mark.parametrize("text, cat", TRIGGER)
def test_trigger_texts_raise_their_category(text, cat):
    e, sink = engine()
    evs = e.feed(row("t1", text, 1000))
    assert [ev.category for ev in evs] == [cat], [h.rule_id for h in sink.hits]
    assert evs[0].traffic_ids == ["t1"] and evs[0].ruleset_hash == e.ruleset_hash()


@pytest.mark.parametrize("text", QUIET)
def test_quiet_texts_raise_nothing(text):
    e, sink = engine()
    assert e.feed(row("t1", text, 1000)) == [] and sink.pushes == []


def test_out_of_scope_rows_are_never_read():
    e, sink = engine()
    for r in (row("f1", "smoke showing from the house", 1, source="tg:307", system="westchester", service="fire"),
              row("f2", "smoke showing from the house", 1, service="ems"),
              row("f3", "smoke showing from the house", 1, source="fm:FG1", system="analog", service="fire")):
        assert e.feed(r) == []
    assert sink.hits == []


def test_every_hit_is_reported_shadow_and_below_threshold_too():
    e, sink = engine({"intel_rules": {"pd.smell_smoke": {"mode": "shadow"}}})
    e.feed(row("t1", "odor of smoke, unknown source", 1000))           # 0.45 alone: below the 0.6 threshold
    e.feed(row("t2", "report of an MVA on Sample St", 1100))          # 0.3 + the cue: below too
    assert {(h.traffic_id, h.rule_id, h.event_id) for h in sink.hits} >= {("t1", "pd.smell_smoke", None),
                                                                          ("t2", "pd.mva", None)}
    assert sink.events == []


# -- the window: one call, several transmissions ---------------------------------------------------------------------

def test_the_call_out_and_the_read_back_are_one_event_and_the_read_back_places_it():
    clock = Clock(5000)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("t1", "report of smoke coming from a house", 5000))
    assert ev.precision == "none" and ev.label_full == ""
    clock.t = 5030
    assert e.feed(row("t2x", "units respond to Sample St and Test Rd", 5020)) == []    # a cue alone: another job's
    [up] = e.feed(row("t2", "units respond to 100 Example Ave for the smoke", 5030))  # the cue that says the smoke
    assert up.id == ev.id and up.precision == "address" and up.traffic_ids == ["t1", "t2"]
    assert len({x[0].id for x in sink.events}) == 1                              # never a second event
    # the place first heard 30 s after the first push: pushed once more, whatever repush_min (10) says (M1)
    assert [d.would_push for _e, d in sink.pushes] == [True, True] and sink.pushes[1][1].repush
    assert sink.pushes[1][1].message["body"].startswith("100 Example Ave")
    clock.t = 5100
    e.feed(row("t3", "units respond to 100 Example Ave, smoke now from the roof", 5100))
    assert len([d for _e, d in sink.pushes if d.would_push]) == 2            # nothing new: no third push


def test_a_read_back_with_no_rule_words_confirms_the_place():
    clock = Clock(5000)
    e, sink = engine(clock=clock)
    [ev] = e.feed(row("t1", "smoke coming from the house, maybe 100 Example Ave", 5000, talker="Unit 7"))
    assert ev.precision == "address" and not ev.confirmed
    clock.t = 5040
    assert e.feed(row("t2", "car 5 copy, en route to Sample St and Test Rd", 5040, talker="Unit 5")) == []   # another job
    [up] = e.feed(row("t3", "copy, 100 Example Ave", 5050, talker="Unit 7"))     # the same talker: its read-back
    assert up.id == ev.id and up.confirmed and up.traffic_ids == ["t1", "t3"]


def test_a_place_found_later_pushes_again_once_within_the_repush_limits():
    clock = Clock(5000)
    e, sink = engine({"intel": {"repush_min": 0}}, clock=clock)
    e.feed(row("t1", "report of smoke coming from a house", 5000))
    clock.t = 5030
    e.feed(row("t2", "respond to 100 Example Ave for the smoke coming from the house", 5030))
    pushed = [d for _e, d in sink.pushes if d.would_push]
    assert [d.repush for d in pushed] == [False, True]
    assert pushed[0].message["body"].startswith("location not heard")
    assert pushed[1].message["body"].startswith("100 Example Ave")           # the exact address (user, 2026-11-12)
    assert pushed[1].message["title"] == "PD · Smoke / fire reported · Port Chester"


def test_the_same_incident_mentioned_again_updates_and_never_pushes_again():
    clock = Clock(1000)
    e, sink = engine(clock=clock)
    [a] = e.feed(row("t1", "smoke coming from the roof", 1000))
    for k, t in enumerate((1300, 1600, 2000)):                      # "still smoke showing" every few minutes (H3)
        clock.t = t
        out = e.feed(row(f"t{k + 2}", "still smoke showing from the roof", t))
        assert [x.id for x in out] in ([a.id], [])
    assert sum(d.would_push for _e, d in sink.pushes) == 1
    clock.t = 1000 + 1000 + 21 * 60                                 # past merge_min (20) after the last: a new one
    [b] = e.feed(row("t9", "smoke coming from the roof", clock.t))
    assert b.id != a.id


def test_the_same_place_from_another_source_merges():
    e, _sink = engine()
    [a] = e.feed(row("t1", "smoke coming from 100 Example Ave", 1000))
    [b] = e.feed(row("t2", "smoke coming from 100 Example Ave", 1010, source="p25:mrrs:9212"))
    assert a.id == b.id and b.traffic_ids == ["t1", "t2"]


def test_disregard_ends_the_open_event():
    e, sink = engine()
    [a] = e.feed(row("t1", "shots fired near Sample St and Test Rd", 1000))
    out = e.feed(row("t2", "disregard, units disregard", 1020))
    assert [x.status for x in out] == ["ended"] and sink.events[-1][1] == "ended"


def test_events_expire_at_their_ttl():
    clock = Clock(1000)
    e, sink = engine(clock=clock)
    [a] = e.feed(row("t1", "vehicle pursuit, failed to stop", 1000))
    assert e.tick(1000 + 19 * 60) == []
    [x] = e.tick(1000 + 21 * 60)
    assert x.id == a.id and x.status == "expired" and sink.events[-1][1] == "expired"


# -- idempotent per traffic id ----------------------------------------------------------------------------------------

def test_the_same_row_again_changes_nothing_and_a_retext_replaces_its_hits():
    class Clearing(RecordingSink):
        def __init__(self):
            super().__init__()
            self.cleared = []

        def clear(self, tid, keep):
            self.cleared.append((tid, sorted(keep)))
    sink = Clearing()
    e = IntelEngine(settings={}, events=MemoryEvents(), sink=sink, locate=example_locate, thread=False, clock=Clock())
    r = row("t1", "respond to 100 Example Ave, smoke coming from the house", 1000)
    [ev] = e.feed(r)
    n_events, n_push = len(sink.events), len(sink.pushes)
    assert e.feed(dict(r)) == [] and e.feed(dict(r)) == []
    assert len(sink.events) == n_events and len(sink.pushes) == n_push
    e.feed(dict(r, text="respond to 100 Example Ave, parking complaint"))     # re-transcribed: no smoke any more
    assert sink.cleared[-1] == ("t1", ["pd.dispatch_cue", "pd.routine"])
    assert len([d for _e, d in sink.pushes if d.would_push]) == 1


# -- the mode ladder, the kill switch, the caps -----------------------------------------------------------------------

def test_modes_cap_from_the_pack_the_category_and_the_rule():
    for ov, want in (({"intel_packs": {"police": {"mode": "map"}}}, "map"),
                     ({"intel_packs": {"police": {"categories": {"weapons": {"mode": "log"}}}}}, "log"),
                     ({"intel_rules": {"pd.shots": {"mode": "shadow"}}}, None),     # shadow rule: counted, not scored
                     ({"intel_rules": {"pd.shots": {"mode": "off"}}}, None),
                     ({"intel_packs": {"police": {"categories": {"weapons": {"mode": "off"}}}}}, None)):
        e, sink = engine(ov)
        evs = e.feed(row("t1", "shots fired, shots fired", 1000))
        assert [x.mode for x in evs] == ([want] if want else []), ov
        assert not any(d.would_push for _e, d in sink.pushes)


def test_the_kill_switch_stops_pushes_and_detection_goes_on():
    e, sink = engine({"intel": {"push": False}})
    [ev] = e.feed(row("t1", "shots fired at Sample St and Test Rd", 1000))
    [(_e, d)] = sink.pushes
    assert not d.would_push and d.suppressed == "off" and ev.suppressed == "off" and ev.mode == "push"
    e2, _ = engine({"intel": {"enabled": False}})
    assert e2.feed(row("t1", "shots fired", 1000)) == []


def test_rate_caps_hold_pushes_and_a_digest_counts_them():
    clock = Clock(10_000)
    e, sink = engine({"intel": {"per_hour": 2, "digest_min": 15}}, clock=clock)
    for i in range(4):
        clock.t = 10_000 + i * 300
        e.feed(row(f"t{i}", "shots fired", clock.t, source=f"p25:mrrs:92{i}"))        # four sources: four events
    ds = [d for _e, d in sink.pushes]
    assert [d.would_push for d in ds] == [True, True, False, False] and ds[2].suppressed == "rate"
    e.tick(10_000 + 3 * 300 + 15 * 60)
    assert len(sink.digests) == 1 and len(sink.digests[0]) == 2


def test_quiet_hours():
    p = Policy({"quiet_from": "23:00", "quiet_to": "06:00"})
    from datetime import datetime, timezone
    late = datetime(2026, 11, 12, 23, 30, tzinfo=timezone.utc).timestamp()
    noon = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc).timestamp()
    p.tz = timezone.utc
    ev = Event(id=1, category="weapons", mode="push", status="open", priority=5)
    assert p.decide(ev, "raised", late).suppressed == "quiet"
    assert p.decide(ev, "raised", noon).would_push


# -- the push: allow-list and the scrub -------------------------------------------------------------------------------

def test_the_push_carries_the_exact_address_and_never_transcript_words():
    e, sink = engine()
    e.public_url = "https://sizeup.example.org"
    e.feed(row("t1", "respond to 100 Example Ave, caller Jane Example reports smoke coming from the house", 1000,
               talker="Officer Example", radio_id="7654321"))
    [(ev, d)] = [(x, d) for x, d in sink.pushes if d.would_push]
    m = d.message
    assert m["title"] == "PD · Smoke / fire reported · Port Chester"
    assert m["body"].startswith("100 Example Ave · ") and m["body"].endswith("· medium")
    assert m["tags"] == ["fire", "police_car"] and m["priority"] == 4
    assert m["click"] == f"https://sizeup.example.org/radio?view=police&intel={ev.id}"
    blob = json.dumps(m).lower()
    for word in ("jane", "caller", "officer", "7654321", "respond", "reports"):
        assert word not in blob


def test_unconfirmed_places_say_so():
    ev = Event(id=3, domain="police", category="weapons", precision="address", label_full="100 Example Ave, Rye",
               town="Rye", confirmed=False, first_at=0, level="high", priority=5)
    m = push_mod.compose(ev, "PD", {"label": "Weapon / shots", "tag": "warning"})
    assert m["body"].startswith("Example Ave, 100 blk (unconfirmed) · ")        # never the house number (H1)


@pytest.mark.parametrize("label, forbidden, why", [
    ("100 Example Ave, Port Chester", ("Officer Sample",), ""),
    ("Officer Sample Park, Port Chester", ("Officer Sample",), "talker"),
    ("1234567 Example Ave", (), "radio_id"),
    ("ABC1234 Example Ave", (), "plate"),
    ("DOB 01/02/1990 Example Ave", (), "date"),
    ("266 Example Ave, Rye", ("266",), ""),         # a number alone is never "a talker": it may be the house number
])
def test_the_scrub_fails_closed(label, forbidden, why):
    ev = Event(id=1, domain="police", category="weapons", precision="address", label_full=label, confirmed=True,
               first_at=0, level="high", priority=5)
    assert push_mod.scrub(push_mod.compose(ev, "PD", {"label": "Weapon / shots"}), forbidden) == why


def test_a_scrubbed_push_is_withheld_and_said_so():
    def bad_locate(text, scope):
        return Place("landmark", 41, -73, 75, "Officer Sample Park, Rye", "Rye", True, 0.9)
    e, sink = engine(locate=bad_locate)
    [ev] = e.feed(row("t1", "shots fired by the park", 1000, talker="Officer Sample"))
    assert ev.precision == "none"                                    # a label naming the talker is no place (L7)
    [(_e, d)] = sink.pushes
    assert d.would_push and "Officer" not in d.message["title"] + d.message["body"]

    def bad_town(text, scope):
        return Place("landmark", 41, -73, 75, "Example Park", "Officer Sample", True, 0.9)
    e, sink = engine(locate=bad_town)
    [ev] = e.feed(row("t1", "shots fired by the park", 1000, talker="Officer Sample"))
    [(_e, d)] = sink.pushes
    assert not d.would_push and d.suppressed == "scrub" and ev.suppressed == "scrub" and ev.pushed_at is None


# -- the locator: police tokens never read as house numbers; 266 stays police ---------------------------------------

def test_police_tokens_are_taken_out_before_the_gazetteer():
    s = place_mod.strip("266 car 12, 10-4, respond to 40 Example Ave, plate ABC1234 at 14:32", "mrrs", "police")
    assert "266" not in s.split() and "12" not in s.split() and "ABC1234" not in s and "14:32" not in s
    assert "40 Example Ave" in s


@pytest.mark.parametrize("text", ["DPT266 to command", "CHIEF266 on scene", "2662 responding", "Engine 266 responding"])
def test_police_266_is_never_rye_brook_fds(text):
    # the police callsign is removed only as the number alone on the police system; fire's tokens are left as they are
    out = place_mod.strip(text, "mrrs", "police").split()
    assert "266" not in out and (text.split()[0] in out or not text.split()[0][0].isdigit())
    assert place_mod.strip("266 respond", "westchester", "fire") == "266 respond"     # another system: not ours to strip
    assert "266" in place_mod.callsigns("mrrs", "police") and "266" not in place_mod.callsigns("mrrs", "fire")


def test_a_fire_row_naming_266_never_reaches_the_police_pack():
    e, sink = engine()
    fire = row("f1", "DPT266 CHIEF266 2662 shots fired", 1000, source="tg:307", system="westchester", service="fire")
    assert e.feed(fire) == [] and sink.hits == []


def test_the_real_locator_on_the_gazetteer_keeps_266_off_the_address():
    from sizeup.geo.gazetteer import DEFAULT_DB, Gazetteer
    if not DEFAULT_DB.exists():
        pytest.skip("no gazetteer")
    loc = place_mod.Locator(Gazetteer())
    p = loc("266 car 3 respond to 40 Example Ave for smoke", {"system": "mrrs", "service": "police",
                                                                    "towns": ["Port Chester", "Rye", "Rye Brook"]})
    assert p is None or not p.label.startswith("266 ")
    assert loc("copy that, 10-4", {"system": "mrrs", "service": "police"}) is None       # no cue: no gazetteer work


# -- the CAD: the invariants, live --------------------------------------------------------------------------------------

class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, body, priority=3, tags=(), click="", who=None):
        self.sent.append({"title": title, "body": body, "priority": priority, "tags": list(tags), "click": click})
        return True


def ctx_row(tid, text, at):
    return {"id": tid, "start": at, "end": at + 3, "source": "MRRS PD", "talkgroup": 9211, "system": "mrrs",
            "source_key": KEY, "service": "police", "tier": "alert", "unit": "", "text": text, "status": "done",
            "model": "whisper"}


def counts(cad):
    return {t: cad.store.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in ("incidents", "radio_links", "incoming", "alarms")}


@pytest.fixture
def live(tmp_path):
    cad = make_cad(tmp_path)
    n = FakeNotifier()
    cad.intel.notifier, cad.intel.locate = n, example_locate
    return cad, n


def test_live_a_context_row_becomes_an_event_and_one_intel_push(live):
    cad, n = live
    cad.comms.record(ctx_row("c1-t0", "respond to 100 Example Ave, smoke coming from the house", cad.clock()))
    cad.comms.on_context_text("c1-t0")                     # what comms.py calls once PR 0 lands
    assert cad.intel.drain() == 1
    [ev] = cad.intel.events.open_events()
    assert ev.category == "fire_report" and ev.label_full == "100 Example Ave, Port Chester"
    assert [m["title"] for m in n.sent] == ["PD · Smoke / fire reported · Port Chester"]
    hits = cad.store.q("SELECT rule_id, event_id FROM intel_hits WHERE traffic_id = 'c1-t0' ORDER BY rule_id")
    assert [(h["rule_id"], h["event_id"]) for h in hits] == [("pd.dispatch_cue", ev.id), ("pd.smoke_seen", ev.id)]
    cad.comms.on_context_text("c1-t0")                     # again (a re-run with the same text): nothing new
    cad.intel.drain()
    assert len(n.sent) == 1 and len(cad.intel.events.open_events()) == 1


def test_intel_never_writes_incidents_links_incoming_or_alarms(live):
    cad, n = live
    before = counts(cad)
    for i, text in enumerate(t for t, _c in TRIGGER):
        tid = f"c{i}-t0"
        cad.comms.record(ctx_row(tid, text, cad.clock() + i * 300))
        cad.intel.consider(tid)
    cad.intel.drain()
    cad.intel.tick(cad.clock() + 10 ** 6)
    assert len(n.sent) >= len(TRIGGER) - 1
    assert counts(cad) == before


def test_context_rows_never_reach_the_fire_consumers(live, monkeypatch):
    cad, _n = live
    seen = []
    monkeypatch.setattr(cad.radiolog, "consider", lambda tid: seen.append(("radiolog", tid)))
    monkeypatch.setattr(cad.hints, "consider", lambda tid: seen.append(("hints", tid)) or [])
    monkeypatch.setattr(cad.sources, "consider_radio", lambda tid: seen.append(("sources", tid)))
    q = []
    monkeypatch.setattr(cad.intel, "consider", lambda tid: q.append(tid))
    cad.comms.on_context_text = cad.intel.consider
    cad.comms.record(ctx_row("c9-t0", "shots fired", cad.clock()))
    assert seen == []                                      # record() of a context row: no on_text at all
    cad.comms.on_context_text("c9-t0")
    assert q == ["c9-t0"] and seen == []


def test_pushes_only_from_the_leader(live):
    cad, n = live
    cad.intel.leader = lambda: False
    cad.comms.record(ctx_row("c1-t0", "shots fired at Sample St and Test Rd", cad.clock()))
    cad.intel.consider("c1-t0")
    cad.intel.drain()
    assert n.sent == [] and cad.intel.events.open_events() == []         # a standby does nothing at all
    # ... and the sink checks again right before it sends (the lease lost between the verdict and the push)
    assert isinstance(cad.intel.sink, LiveSink)
    calls = iter([True])                                   # the thread's check passes, the sink's doesn't
    cad.intel.leader = lambda: next(calls, False)
    cad.intel.consider("c1-t0")
    cad.intel.drain()
    assert n.sent == [] and len(cad.intel.events.open_events()) == 1


def test_the_policy_bucket_survives_a_handover(tmp_path):
    cad = make_cad(tmp_path)
    cad.intel.notifier = FakeNotifier()
    cad.comms.record(ctx_row("c1-t0", "shots fired", cad.clock()))
    cad.intel.consider("c1-t0")
    cad.intel.drain()
    saved = json.loads(cad.store.meta_get("intel_policy"))
    assert len(saved["sent"]) == 1
    cad2 = make_cad(tmp_path)
    cad2.intel._restore()
    assert len(cad2.intel.policy.sent) == 1


# -- replay: the same pipeline, nothing written ------------------------------------------------------------------------

def test_replay_reads_a_read_only_database_and_writes_nothing(tmp_path, monkeypatch):
    cad = make_cad(tmp_path)
    for i, (text, _c) in enumerate(TRIGGER):
        cad.comms.record(ctx_row(f"c{i}-t0", text, 1000 + i * 400))
    cad.settings._commit([("intel", "per_hour", 3)], "test", [("system", "intel/per_hour", None, 3)])
    path = tmp_path / "cad.sqlite"
    cad.store.db.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(notify, "publish", lambda *a, **k: pytest.fail("replay published"))
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    snap = {s: {r["key"]: json.loads(r["value"]) for r in ro.execute(
        "SELECT key, value FROM settings_overrides WHERE section = ?", (s,))} for s in ("intel", "intel_packs", "intel_rules")}
    sink = RecordingSink()
    clock = Clock(0)
    e = IntelEngine(settings=snap, events=MemoryEvents(), sink=sink, locate=example_locate, thread=False, clock=clock)
    for r in ro.execute("SELECT id, start, end, source, system, service, dept, tier, talkgroup, channel, talker, rig, "
                        "radio_id, text, alt_text, model FROM radio_traffic ORDER BY start"):
        clock.t = r["start"]
        e.feed(dict(r))
    e.tick(10 ** 6)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO intel_hits (traffic_id, rule_id, rule_rev, weight, category, pack, at) "
                   "VALUES ('x', 'y', 1, 1, 'c', 'p', 0)")
    ro.close()
    assert e.policy.cfg["per_hour"] == 3
    assert len({ev.id for ev, _c in sink.events}) == len(TRIGGER)
    assert sum(d.would_push for _e, d in sink.pushes) == 3            # 400 s apart: the snapshot's 3 an hour
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


# -- the API: signed in only; dismiss, feedback, Settings ------------------------------------------------------------------

@pytest.fixture
def rig(tmp_path):
    cad = make_cad(tmp_path)
    people(cad)
    cad.intel.notifier, cad.intel.locate = FakeNotifier(), example_locate
    cad.comms.record(ctx_row("c1-t0", "shots fired at Sample St and Test Rd", cad.clock()))
    cad.intel.consider("c1-t0")
    cad.intel.drain()
    return cad, CadApi(cad)


def get(api, path, query="", who=COREY):
    code, raw, _ = api.get(path, query, Identity(email=who) if who else None)
    return code, json.loads(raw)


def test_events_api_is_for_anyone_signed_in_and_nobody_else(rig):
    cad, api = rig
    assert get(api, "/api/cad/intel/events", who=None)[0] == 403
    for who in (COREY, JULIAN, VAL):
        code, out = get(api, "/api/cad/intel/events", "mode=map,push", who)
        assert code == 200, out
        [ev] = out["events"]
        assert ev["category"] == "weapons" and ev["precision"] == "intersection" and ev["lat"] == 41.01
        assert set(ev) >= {"id", "label", "what", "level", "where", "confirmed", "radius_m", "place", "alts", "mode",
                           "first_at", "last_at", "until", "traffic_ids", "status"}
        assert "text" not in json.dumps(ev).lower().replace("traffic_ids", "")
    assert get(api, "/api/cad/intel/events", "bbox=-73.6,41.1,-73.5,41.2")[1]["events"] == []
    assert len(get(api, "/api/cad/intel/events", "bbox=-73.7,41.0,-73.6,41.1")[1]["events"]) == 1


def test_the_console_state_carries_open_events_only_for_signed_in_viewers(rig):
    cad, _api = rig
    assert cad.state()["intel"] is None                    # the broadcast stream, not behind Access: nothing
    cad.comms.context_broadcast = True
    assert [e["category"] for e in cad.state()["intel"]["open"]] == ["weapons"]


def test_dismiss_is_a_dispatchers_and_feedback_anyones(rig):
    cad, api = rig
    [ev] = cad.intel.events.open_events()
    assert needs(f"/api/cad/intel/{ev.id}/dismiss") == "dispatch" and needs(f"/api/cad/intel/{ev.id}/feedback") == "read"
    assert call(api, f"/api/cad/intel/{ev.id}/feedback", {"verdict": "useful"}, VAL)[0] == 200
    assert call(api, f"/api/cad/intel/{ev.id}/feedback", {"verdict": "great"}, VAL)[0] == 400
    assert call(api, f"/api/cad/intel/{ev.id}/dismiss", {}, VAL)[0] == 403
    assert call(api, f"/api/cad/intel/{ev.id}/dismiss", {}, JULIAN)[0] == 200
    assert cad.intel.events.open_events() == []
    rows = cad.store.q("SELECT verdict, operator, rules FROM intel_feedback ORDER BY id")
    assert [r["verdict"] for r in rows] == ["useful", "noise"] and all("Julian" in r["operator"] or "Val" in r["operator"] for r in rows)
    assert counts(cad)["incidents"] == 0


def test_settings_change_modes_caps_and_rules_audited_and_checked(rig):
    cad, api = rig
    code, out = get(api, "/api/cad/intel")
    assert code == 200 and out["settings"]["push"] is True and out["packs"][0]["name"] == "police"
    assert {r["id"]: r for r in out["packs"][0]["rules"]}["pd.shots"]["hits"] == 1
    post = lambda b, who=COREY: call(api, "/api/cad/settings/intel", b, who)      # noqa: E731
    assert post({"action": "set", "key": "push", "value": False}, JULIAN)[0] == 403
    assert post({"action": "set", "key": "push", "value": False})[0] == 200 and cad.intel.policy.cfg["push"] is False
    assert post({"action": "set", "key": "per_hour", "value": 0})[0] == 400
    assert post({"action": "set", "key": "per_hour", "value": 60})[0] == 200 and cad.intel.policy.cfg["per_hour"] == 60
    assert post({"action": "pack", "pack": "police", "threshold": 0.8})[0] == 200 and cad.intel.pack("police").threshold == 0.8
    assert post({"action": "category", "pack": "police", "category": "alarm", "mode": "shadow"})[0] == 200
    assert cad.intel.pack("police").categories["alarm"]["mode"] == "shadow"
    assert post({"action": "rule", "rule": "pd.shots", "w": 0.9, "mode": "map"})[0] == 200
    code, out = post({"action": "local", "rule": "local.bad", "pack": "police", "category": "weapons", "any": ["(open"]})
    assert code == 400 and "unbalanced" in out["error"]
    assert post({"action": "local", "rule": "local.knife", "pack": "police", "category": "weapons",
                 "any": ["knife ... (pulled|out)"]})[0] == 200
    r = next(x for x in cad.intel.pack("police").rules if x.id == "local.knife")
    assert r.mode == "shadow"                                     # an admin's new rule starts in shadow
    assert post({"action": "reset", "what": "category", "pack": "police", "category": "alarm"})[0] == 200
    assert cad.intel.pack("police").categories["alarm"]["mode"] == "push"
    audit = cad.settings_audit_log({"section": ["system"]})["audit"]
    keys = {a["key"] for a in audit}
    assert {"intel/push", "intel/per_hour", "intel_packs/police", "intel_packs/police/alarm", "intel_rules/pd.shots",
            "intel_rules/local.knife"} <= keys
    ex = cad.settings_export()
    assert "intel_rules" in ex["overrides"]


def test_the_section_is_in_settings_system_and_the_card_is_on_the_console():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "sizeup" / "web" / "static"
    html = (root / "cad.html").read_text()
    assert 'src="/static/intel.js"' in html and 'id="q-intel"' in html
    js = (root / "intel.js").read_text()
    assert "IntelSettings" in js and "IntelCard" in js
    assert "IntelSettings.mount" in (root / "settings.js").read_text()


def test_sql_and_memory_events_round_trip(tmp_path):
    cad = make_cad(tmp_path)
    for store in (MemoryEvents(), SqlEvents(cad.store)):
        ev = store.save(Event(pack="police", category="weapons", subject="s", first_at=1, last_at=2, until=3,
                              rules=[["pd.shots", 1, 0.75]], traffic_ids=["a", "b"], confirmed=True))
        got = store.get(ev.id)
        assert got.rules == [["pd.shots", 1, 0.75]] and got.traffic_ids == ["a", "b"] and got.confirmed is True
        assert [e.id for e in store.open_events()] == [ev.id]


def test_may_see_context_is_the_gate():
    assert sourcekeys.may_see_context(False) is False and sourcekeys.may_see_context(True) is True


# -- the on_context_text hook's contract (#483 review): it fires again, on the caller's thread ------------------------

def test_a_startup_backfill_of_old_rows_pushes_nothing(live):
    cad, n = live
    now = cad.clock()
    for i, (text, _c) in enumerate(TRIGGER):
        tid = f"old{i}-t0"
        cad.comms.record(ctx_row(tid, text, now - 3600 - i))          # an hour old: the backfill replays them
        cad.comms.on_context_text(tid)
    cad.intel.drain()
    assert n.sent == [] and cad.intel.events.open_events() == [] and cad.intel.stale == len(TRIGGER)
    cad.intel_admin.settings_op("set", {"key": "max_age_min", "value": 120}, "test")     # configurable
    cad.comms.on_context_text("old0-t0")
    cad.intel.drain()
    assert len(n.sent) == 1


def test_the_hook_firing_again_for_the_same_text_does_nothing(live):
    cad, n = live
    cad.comms.record(ctx_row("c1-t0", "shots fired at Sample St and Test Rd", cad.clock()))
    for _ in range(5):                                                 # every re-record with replace=True
        cad.comms.on_context_text("c1-t0")
    cad.intel.drain()
    assert len(n.sent) == 1 and len(cad.intel.events.open_events()) == 1


def test_consider_only_enqueues_on_the_callers_thread(live, monkeypatch):
    cad, n = live
    called = []
    monkeypatch.setattr(cad.intel, "feed", lambda row: called.append(row))
    monkeypatch.setattr(cad.intel, "_one", lambda tid: called.append(tid))
    with cad.store.lock:                                               # even under the caller's store lock
        cad.comms.on_context_text("c1-t0")
    assert called == [] and n.sent == [] and cad.intel._q.qsize() == 1


# -- the #492 review ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, gone", [
    ("vehicle New York ABC 1234 heading down Example Ave", "1234"),
    ("plate ABC-1234 heading down Example Ave", "1234"),
    ("silver sedan ABC 1234 on Example Ave", "1234"),
    ("ID 1452 Example Ave", "1452"),
    ("radio 1452 at Example Ave", "1452"),
    ("DOB 01/02/1990, lives on Example Ave", "1990"),
])
def test_h1_plate_and_id_digits_never_reach_the_gazetteer(text, gone):
    out = place_mod.strip(text, "mrrs", "police")
    assert gone not in out and "Example Ave" in out
    assert "100" in place_mod.strip("unit 3 at 100 Example Ave", "mrrs", "police")      # only the unit's number goes


def test_h1_a_number_said_after_an_id_word_is_never_pushed_as_the_address():
    def plate_locate(text, scope):
        return Place("address", 41.0, -73.66, 25.0, "1234 Example Ave, Port Chester", "Port Chester", True, 0.9)
    e, sink = engine(locate=plate_locate)
    e.feed(row("t1", "shots fired, suspect vehicle ID 1234 Example Ave", 1000))
    [(_e, d)] = [(x, d) for x, d in sink.pushes if d.would_push]
    assert "1234" not in d.message["body"] and d.message["body"].startswith("Example Ave · ")
    assert push_mod.scrub({"title": "", "body": "ABC 1234 Example Ave"}) == "plate"


def test_h2_disregard_then_a_routine_cue_never_pushes_again():
    clock = Clock(1000)
    e, sink = engine(clock=clock)
    e.feed(row("t1", "shots fired at Sample St and Test Rd", 1000))
    e.feed(row("t2", "disregard, units disregard", 1010))
    for k, text in enumerate(("respond to Sample St", "report of shots fired at Sample St and Test Rd")):
        clock.t = 1020 + k * 60
        e.feed(row(f"t{3 + k}", text, clock.t))
    assert sum(d.would_push for _e, d in sink.pushes) == 1
    clock.t = 1010 + 16 * 60                                     # past mute_min (15): a new mention is a new event
    assert e.feed(row("t9", "shots fired at Sample St and Test Rd", clock.t))


def test_h2_a_dismissed_event_doesnt_come_straight_back(live):
    cad, n = live
    cad.comms.record(ctx_row("c1-t0", "shots fired at Sample St and Test Rd", cad.clock()))
    cad.intel.consider("c1-t0")
    cad.intel.drain()
    [ev] = cad.intel.events.open_events()
    cad.intel_admin.dismiss(ev.id, {"operator": "Lt. Test"})
    cad.comms.record(ctx_row("c2-t0", "units respond, shots fired at Sample St and Test Rd", cad.clock() + 5))
    cad.intel.consider("c2-t0")
    cad.intel.drain()
    assert len(n.sent) == 1 and [x.mode for x in cad.intel.events.open_events()] == ["log"]   # recorded, never shown


def test_h4_intel_writes_never_bump_the_cad_rev(live):
    cad, n = live
    cad.comms.record(ctx_row("c1-t0", "respond to 100 Example Ave, smoke coming from the house", cad.clock()))
    rev, irev = cad.store.rev, cad.intel.events.rev
    cad.intel.consider("c1-t0")
    cad.intel.drain()
    assert n.sent and cad.store.rev == rev and cad.intel.events.rev > irev
    cad.comms.context_broadcast = True
    assert cad.state()["intel"]["open"]                          # the card reads by intel's own rev


def test_m3_the_kill_switch_enabled_and_quiet_hours_stop_the_digest():
    for extra in ({"push": False}, {"quiet_from": "00:00", "quiet_to": "23:59"}):
        clock = Clock(10_000)
        e, sink = engine({"intel": {"per_hour": 1, "digest_min": 15}}, clock=clock)
        for i in range(3):
            clock.t = 10_000 + i * 60
            e.feed(row(f"t{i}", "shots fired", clock.t, source=f"p25:mrrs:92{i}"))
        e.policy.configure({"per_hour": 1, "digest_min": 15, **extra})
        e.tick(10_000 + 3600)
        assert sink.digests == [], extra


def test_m4_the_rules_are_admins_only_and_l3_shadow_events_too(rig):
    cad, api = rig
    assert get(api, "/api/cad/intel", who=JULIAN)[0] == 403 and get(api, "/api/cad/intel", who=VAL)[0] == 403
    assert get(api, "/api/cad/intel", who=COREY)[0] == 200
    cad.intel_admin.settings_op("category", {"pack": "police", "category": "alarm", "mode": "shadow"}, "test")
    cad.comms.record(ctx_row("c7-t0", "commercial fire alarm activation at the plaza", cad.clock() + 1))
    cad.intel.consider("c7-t0")
    cad.intel.drain()
    viewer = get(api, "/api/cad/intel/events", "mode=shadow,log,map,push&status=all", VAL)[1]["events"]
    admin = get(api, "/api/cad/intel/events", "mode=shadow,log,map,push&status=all", COREY)[1]["events"]
    assert all(e["mode"] in ("map", "push") for e in viewer) and any(e["mode"] == "shadow" for e in admin)


def test_m5_the_first_prune_waits_hours_and_l2_a_forwarding_station_queues_nothing(tmp_path):
    cad = make_cad(tmp_path)
    from sizeup.intel.engine import PRUNE_FIRST_S, PRUNE_S
    assert cad.intel._pruned - cad.clock() >= PRUNE_FIRST_S - PRUNE_S - 5
    cad.intel.active = lambda: False
    cad.intel.consider("x")
    assert cad.intel._q.qsize() == 0


def test_l1_counted_only_once_the_sink_takes_it():
    class Refusing(RecordingSink):
        def push(self, ev, d):
            super().push(ev, d)
            return False
    e = IntelEngine(settings={}, events=MemoryEvents(), sink=Refusing(), locate=example_locate, thread=False, clock=Clock())
    [ev] = e.feed(row("t1", "shots fired", 1000))
    assert ev.pushed_at is None and ev.push_count == 0 and e.policy.sent == [] and ev.suppressed == "unsent"


def test_l6_the_hash_covers_towns_scope_and_context_factor():
    d = json.loads((rules_mod.DATA / "police.json").read_text())
    h = rules_mod.ruleset_hash(rules_mod.load_packs([d]))
    for k, v in (("towns", {"p25:mrrs:9211": ["Rye"]}), ("context_factor", 0.2), ("scope", {"services": ["police"]})):
        assert rules_mod.ruleset_hash(rules_mod.load_packs([dict(d, **{k: v})])) != h, k


# -- the #492 re-check: merge, mute and same-job ----------------------------------------------------------------------

def pushed(sink):
    return [(e.category, d.message["body"].split(" · ")[0]) for e, d in sink.pushes if d.would_push]


def test_r1_a_new_place_or_a_new_call_is_a_new_incident():
    clock = Clock(1000)
    e, sink = engine(clock=clock)
    [a] = e.feed(row("t1", "shots fired at 100 Example Ave", 1000))
    clock.t = 1480                                                    # 8 min later: another call, another place
    [b] = e.feed(row("t2", "new call, shots fired at Sample St and Test Rd", 1480))
    assert b.id != a.id and b.label_full.startswith("Sample St")
    clock.t = 1600
    [c] = e.feed(row("t3", "shots fired at Sample St and Test Rd", 1600, source="p25:mrrs:9212"))   # the same place
    assert c.id == b.id
    clock.t = 1700
    [d] = e.feed(row("t4", "shots fired again at 100 Example Ave", 1700))       # no "new call", but another place
    assert d.id == a.id
    assert [x[0] for x in pushed(sink)] == ["weapons", "weapons"]


def test_r2_an_unrelated_disregard_ends_nothing_and_mutes_nothing():
    clock = Clock(1000)
    e, sink = engine(clock=clock)
    [a] = e.feed(row("t1", "shots fired at 100 Example Ave", 1000))
    clock.t = 1300
    assert e.feed(row("t2", "car 4, disregard the lockout call", 1300)) == []
    assert e.events.get(a.id).status == "open"
    clock.t = 1400
    [g] = e.feed(row("t3", "male with a gun at Sample St and Test Rd", 1400))
    assert g.id != a.id and g.mode == "push" and len(pushed(sink)) == 2


def test_r2_the_mute_is_by_place_never_by_source():
    clock = Clock(1000)
    e, sink = engine(clock=clock)
    e.feed(row("t1", "shots fired at 100 Example Ave", 1000))
    e.feed(row("t2", "disregard, units disregard, shots fired call cancelled", 1010))
    clock.t = 1100
    [same] = e.feed(row("t3", "shots fired at 100 Example Ave", 1100))      # the same place a moment later: logged
    assert same.mode == "log"
    clock.t = 1110
    [other] = e.feed(row("t4", "shots fired at Sample St and Test Rd", 1110))   # another place: a real pre-alert
    assert other.mode == "push" and len(pushed(sink)) == 2


def test_r3_the_dispatchers_next_job_never_places_this_one():
    clock = Clock(1000)
    e, sink = engine(clock=clock)
    [a] = e.feed(row("t1", "shots fired, caller couldn't say where", 1000, talker="DISP"))
    clock.t = 1030
    assert e.feed(row("t2", "car 5, Sample St and Test Rd, lost wallet", 1030, talker="DISP")) == []
    assert e.events.get(a.id).precision == "none" and len(pushed(sink)) == 1        # no place, no repush
    clock.t = 1040
    assert e.feed(row("t3", "car 6, Sample St and Test Rd", 1040, talker="")) == []   # a blank talker: no tie either
    assert e.events.get(a.id).precision == "none"


def test_r4_a_unit_tied_to_the_event_places_it_from_its_own_report():
    clock = Clock(1000)
    e, sink = engine(clock=clock)
    [a] = e.feed(row("t1", "car 3 respond, shots fired", 1000, talker="DISP"))
    clock.t = 1060
    [up] = e.feed(row("t2", "car 3 to dispatch, I'm at Sample St and Test Rd", 1060, talker=""))
    assert up.id == a.id and up.label_full.startswith("Sample St")
    assert [x[1] for x in pushed(sink)] == ["location not heard", "Sample St & Test Rd"]   # the M1 repush, tied


@pytest.mark.parametrize("text", ["all units go to 120 Example Ave", "house number 120 Example Ave",
                                  "K9 unit 120 Example Ave", "plate is 120 Example Ave"])
def test_r5_real_house_numbers_stay(text):
    assert "120 Example Ave" in place_mod.strip(text, "mrrs", "police")


@pytest.mark.parametrize("label", ["NY 120 at Example Ave", "US 1 and Example Ave", "RT 120 Sample St"])
def test_r5_route_labels_are_no_plates(label):
    assert push_mod.scrub({"title": "", "body": label}) == "" and label.split()[1] in place_mod.strip(label)


def test_end_to_end_a_dispatchers_evening():
    """Two incidents on the police dispatch channel interleaved with routine traffic, a third job disregarded, and units
    reporting where they are: exactly two open events, each at its own place, one ended, three pushes."""
    clock = Clock(1000)
    e, sink = engine(clock=clock)
    script = [
        (1000, "DISP", "car 3 respond to 100 Example Ave, report of smoke coming from the house"),
        (1020, "DISP", "car 7, parking complaint at the plaza"),
        (1040, "", "car 3 copy, en route"),
        (1100, "DISP", "units respond, shots fired at Sample St and Test Rd"),
        (1130, "DISP", "car 9 respond to the lot, commercial fire alarm activation"),
        (1160, "DISP", "car 9 disregard, alarm company cancelled"),
        (1200, "", "car 3 on scene, smoke showing from the roof"),
        (1260, "", "car 5 to dispatch, I'm at Sample St and Test Rd"),
        (1300, "DISP", "car 7 clear of the parking complaint"),
    ]
    for k, (t, talker, text) in enumerate(script):
        clock.t = t
        e.feed(row(f"s{k}", text, t, talker=talker))
    by = {x.category: x for x in e.events.all()}
    assert set(by) == {"fire_report", "weapons", "alarm"}
    assert by["fire_report"].status == "open" and by["fire_report"].label_full.startswith("100 Example Ave")
    assert by["weapons"].status == "open" and by["weapons"].label_full.startswith("Sample St")
    assert by["alarm"].status == "ended"
    assert set(by["fire_report"].traffic_ids) == {"s0", "s6"} and "s7" not in by["fire_report"].traffic_ids
    assert pushed(sink) == [("fire_report", "100 Example Ave"), ("weapons", "Sample St & Test Rd"), ("alarm", "location not heard")]
