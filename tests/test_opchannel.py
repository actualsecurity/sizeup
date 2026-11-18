"""An incident's operating channel (sizeup/cad/opchannel.py): a call worked on a county talkgroup that isn't our dispatch
talkgroup. Its traffic goes on the incident (by unit, words, or folded "on this channel"), never another call's when that
can be told; the talkgroup is transcribed like ours while the incident is open and a while after.

Invented only: made-up talkgroups (Fire/EMS 31 = TG 4031, Fire/EMS 32 = TG 4032), addresses, times and words."""

from __future__ import annotations

import pytest

from sizeup import channels
from sizeup.app import URGENT, App
from sizeup.cad.api import needs
from sizeup.cad.opchannel import LEAD_S, TAIL_S, parse
from sizeup.cad.store import CadError
from sizeup.incidents import Traffic
from sizeup.stt import LIVE, LOW, RERUN, SttQueue
from tests.test_dispatch_modes import AutoRig
from tests.test_incident_sources import T0

ADDR = "12 Sample Rd"
TG31, TG32 = 4031, 4032
ROWS = [{"tg": 307, "name": "Fire/EMS 15", "group": "Fire/EMS", "system": "westchester"},
        {"tg": TG31, "name": "Fire/EMS 31", "group": "Fire/EMS", "system": "westchester"},
        {"tg": TG32, "name": "Fire/EMS 32", "group": "Fire/EMS", "system": "westchester"},
        {"tg": 4099, "name": "Sample Control", "group": "Fire/EMS", "system": "westchester"},
        {"tg": 4100, "name": "Sample PD 1", "group": "County Police", "system": "westchester"}]
NAMES = {TG31: "Fire/EMS 31", TG32: "Fire/EMS 32"}


@pytest.fixture
def rig(tmp_path):
    r = AutoRig(tmp_path)
    r.cad.source_rows = lambda: ROWS
    return r


def call(rig, eid="A1", addr=ADDR, units=("E60", "L31"), at=None) -> int:
    rig.feed.put(eid, type="STRU", location=addr, units=units, times={"dispatched": T0 if at is None else at})
    rig.poll()
    return rig.iid(eid)


def chans(rig, iid) -> list[tuple]:
    return [(c["tg"], c["source"]) for c in rig.cad.opchan.of([iid])[iid]]


def on31(rig, tid, at, text="", talker="", rig_="", tg=TG31):
    return rig.radio(tid, at, text, tg=tg, source=NAMES.get(tg, "TG"), unit=talker, rig=rig_)


# -- reading the move ---------------------------------------------------------------------------------------------------


def test_the_words_that_move_a_call_and_the_ones_that_dont():
    said = {"All units operate on Fire/EMS 31.": [TG31], "Engine 60 responding, switching to fire 31": [TG31],
            "switch to 31 and stand by": [TG31], "operate on tac 32": [TG32], "go to fire thirty two": [TG32],
            "operate on Fire EMS 31540963": [TG31],                       # a page's number run into what followed
            "go to 31 and stand by": [],                                   # "go to" a number alone: anything
            "switch to 31 Sample Road": [],                                # a house number, never a channel
            "Engine 60 respond to 31 Sample Road": [], "fire 31 is busy": [], "operate on Fire/EMS 15": [],
            "operate on fire 31, then switch to fire 32": [TG31, TG32],
            "Engine 60 going to the fire 31 Sample Road": [],              # "the fire": the call, and a house number
            "head to the fire, 32 Example Avenue": [], "go to ops 32 Main Street": [],
            "go to channel 32": [], "switch to talkgroup 31": [],          # "channel N": our own FM channels too
            "Ladder 31 on fire EMS 32 for the other job": [],              # no moving verb
            "switch to tac 1945": [], "Engine 31 at 32 Sample Road": []}
    names = {**NAMES, 4002: "Fire/EMS 2"}
    for text, want in said.items():
        assert parse(text, NAMES) == want, text
    assert parse("Engine 2 on tac 31", names) == [] and parse("switch to channel 2 on the portable", names) == []
    assert parse("UNITS OPS ON FIRE/EMS 32", NAMES, strict=False) == [TG32]   # a feed's comment needs no verb
    assert parse("FIRE/EMS 32", NAMES, strict=False) == [TG32] and parse("FIRE/EMS 32", NAMES) == []
    assert parse("OPS 32", NAMES, strict=False) == []                     # ... in full


def test_the_channels_are_the_countys_fire_talkgroups_never_ours_or_a_control(rig):
    assert rig.cad.opchan.names() == NAMES
    assert rig.cad.state()["op_channels"] == [{"tg": TG31, "name": "Fire/EMS 31"}, {"tg": TG32, "name": "Fire/EMS 32"}]


# -- from dispatch, its traffic on the incident ---------------------------------------------------------------------------


def test_dispatch_moves_the_call_and_its_traffic_follows_until_it_closes(rig):
    iid = call(rig)
    assert rig.inc()["status"] == "active"
    early = on31(rig, "4031-1-call_1-t0", T0 - 200, "Ladder 31 back in quarters", talker="60 Ctrl OP 3")
    assert early == []
    # 60 Control on our talkgroup, to units on the call: the move
    assert rig.radio("307-1-call_1-t0", T0 + 10, "Engine 60, Ladder 31, operate on Fire/EMS 31") == [iid]
    assert chans(rig, iid) == [(TG31, "dispatch")]
    assert rig.cad.opchan.of([iid])[iid][0]["since"] == T0 + 10 - LEAD_S
    assert "Channel Fire/EMS 31 (dispatch)" in [e["text"] for e in rig.inc()["events"]]
    links = lambda: rig.links(iid)                                            # noqa: E731
    assert on31(rig, "4031-2-call_2-t0", T0 + 30, "", talker="60 Ctrl OP 3") == [iid]       # no text yet: on it
    assert links()["4031-2-call_2-t0"] == ("opchannel", "Fire/EMS 31")
    assert on31(rig, "4031-3-call_3-t0", T0 + 40, "Engine 60 on location", rig_="E60") == [iid]
    assert links()["4031-3-call_3-t0"] == ("unit", "E60")                     # its radio: the unit rule
    assert on31(rig, "4031-4-call_4-t0", T0 + 45, "Ladder 31 to command", talker="L31") == [iid]
    assert links()["4031-4-call_4-t0"] == ("unit", "L31")                     # its county alias
    assert on31(rig, "4031-5-call_5-t0", T0 + 50, "", talker="61X1 P1") == [iid]            # nobody we know: folded
    assert on31(rig, "4031-5-call_5-t0", T0 + 50, "Searching 12 Sample Road", talker="61X1 P1") == [iid]
    assert links()["4031-5-call_5-t0"] == ("mention", "address")              # words: in its place
    assert on31(rig, "4031-6-call_6-t0", T0 + 60, "Engine 60 to command", tg=TG32) == []    # another talkgroup
    # promoted while open, and the tail after it closed
    assert rig.cad.opchan.promoted(T0 + 60) == {TG31}
    rig.at(600)
    rig.cad.close(iid, {"disposition": "OTHER", "operator": "Lt. Test"})
    closed = rig.inc()["closed_at"]
    assert on31(rig, "4031-7-call_7-t0", closed + 30, "Engine 60 available", talker="60 Ctrl OP 3") == []
    assert rig.cad.opchan.promoted(closed + TAIL_S - 1) == {TG31}
    assert rig.cad.opchan.promoted(closed + TAIL_S + 1) == set()
    # a late call (#747/#753): by its own time, not when it came
    assert on31(rig, "4031-8-call_8-t0", T0 + 100, "", talker="60 Ctrl OP 3") == [iid]
    assert not {"4031-1-call_1-t0", "4031-6-call_6-t0", "4031-7-call_7-t0"} & set(links())


def test_another_calls_traffic_on_the_same_channel_stays_off(rig):
    iid = call(rig)
    rig.radio("307-1-call_1-t0", T0 + 10, "Engine 60, operate on Fire/EMS 31")
    assert chans(rig, iid) == [(TG31, "dispatch")]
    # another department's unit (a board unit not on this call), by its alias or its radio
    assert on31(rig, "4031-1-call_1-t0", T0 + 20, "", talker="E191") == []
    assert on31(rig, "4031-2-call_2-t0", T0 + 25, "", rig_="E191") == []
    # words sending someone elsewhere, or naming another open call's address
    other = rig.cad.create({"type": "ALARM", "address": "40 Other Rd", "operator": "Lt. Test"})["id"]
    assert on31(rig, "4031-3-call_3-t0", T0 + 30, "", talker="60 Ctrl OP 3") == [iid]       # nothing known yet
    assert on31(rig, "4031-3-call_3-t0", T0 + 30, "Respond to 400 Example Avenue for the alarm",
                talker="60 Ctrl OP 3") == []                              # its words: another place, taken off
    assert on31(rig, "4031-4-call_4-t0", T0 + 35, "Units at 40 Other Road, all clear", talker="61X1") == [other]
    # kept by a person: never taken off by words that come later
    assert on31(rig, "4031-5-call_5-t0", T0 + 40, "", talker="60 Ctrl OP 3") == [iid]
    rig.cad.keep_traffic(iid, {"traffic_id": "4031-5-call_5-t0", "operator": "Lt. Test"})
    assert on31(rig, "4031-5-call_5-t0", T0 + 40, "Respond to 400 Example Avenue", talker="60 Ctrl OP 3") == [iid]


def test_a_move_for_another_call_never_moves_ours(rig):
    ours = call(rig)
    b = call(rig, "B1", "40 Other Rd", units=("E63",))
    # dispatch's move names E63 (on B1 only): B1's channel, never ours
    assert rig.radio("307-1-call_1-t0", T0 + 10, "Engine 63, operate on Fire/EMS 32") == [b]
    assert chans(rig, b) == [(TG32, "dispatch")] and chans(rig, ours) == []
    assert on31(rig, "4032-1-call_1-t0", T0 + 20, "", talker="60 Ctrl OP 3", tg=TG32) == [b]
    # another department's own move, on our talkgroup, naming none of our calls: nobody's
    assert rig.radio("307-2-call_2-t0", T0 + 30, "Engine 191, operate on Fire/EMS 31") == []
    assert chans(rig, ours) == [] and rig.cad.opchan.promoted() == {TG32}
    # a transmission naming units of two of our calls moves both (a page's event and the feed's own for one call too)
    rig.radio("307-3-call_3-t0", T0 + 40, "Engine 60 and Engine 63, switch to fire 31")
    assert chans(rig, ours) == [(TG31, "dispatch")] and chans(rig, b) == [(TG32, "dispatch"), (TG31, "dispatch")]
    assert on31(rig, "4031-1-call_1-t0", T0 + 50, "", talker="60 Ctrl OP 3") == []        # both on it then: neither


def test_two_of_our_calls_on_one_channel_fold_nothing_but_words_still_link(rig):
    a = call(rig)
    b = call(rig, "B1", "40 Other Rd", units=("E63",))
    rig.cad.incident_channel(a, {"talkgroup": TG31, "operator": "Lt. Test"})
    rig.cad.incident_channel(b, {"talkgroup": TG31, "operator": "Lt. Test"})
    assert on31(rig, "4031-1-call_1-t0", T0 + 20, "", talker="60 Ctrl OP 3") == []
    assert on31(rig, "4031-2-call_2-t0", T0 + 25, "Engine 63 on location", talker="60 Ctrl OP 3") == [b]


# -- from the feed, the units, a person ----------------------------------------------------------------------------------


def test_the_feeds_comment_and_our_units_radios_find_it_too(rig):
    rig.feed.put("A1", type="STRU", location=ADDR, units=("E60", "L31"), times={"dispatched": T0},
                 comments=((T0 + 5, "UNITS OPERATE ON FIRE/EMS 32"),))
    rig.poll()
    a = rig.iid()
    assert chans(rig, a) == [(TG32, "feed")] and rig.cad.opchan.of([a])[a][0]["since"] == T0 + 5 - LEAD_S
    b = call(rig, "B1", "40 Other Rd", units=("E63", "E62"))
    assert on31(rig, "4031-1-call_1-t0", T0 + 30, "Engine 63 to command", rig_="E63") == [b]
    assert on31(rig, "4031-2-call_2-t0", T0 + 35, "", rig_="E63") == [b]
    assert chans(rig, b) == []                                            # one unit keying twice is not enough
    assert on31(rig, "4031-3-call_3-t0", T0 + 40, "", rig_="E62") == [b]
    assert chans(rig, b) == [(TG31, "units")]
    assert rig.cad.opchan.of([b])[b][0]["since"] == T0 + 30 - LEAD_S


def test_a_page_that_moves_the_call(rig):
    rig.feed.outage("failing", "x")
    rig.poll()
    text = ("Port Chester Fire Department, Engine 60, respond to a structure fire. 12 Example Avenue, cross of Sample "
            "Street. All units operate on Fire EMS 32956044. Time out 1432.")
    assert rig.radio("p1", T0, text, tg=None, source="South 2", unit="60 Control") == []   # logged first, as the app does
    inc = rig.page("p1", text=text)
    assert chans(rig, inc["id"]) == [(TG32, "dispatch")]


def test_a_person_adds_and_removes_one_and_a_removal_stands(rig):
    iid = call(rig)
    assert needs(f"/api/cad/incident/{iid}/channel", {}) == "dispatch"
    with pytest.raises(CadError):
        rig.cad.incident_channel(iid, {"talkgroup": 307, "operator": "Lt. Test"})        # ours: never
    with pytest.raises(CadError):
        rig.cad.incident_channel(iid, {"talkgroup": 4100, "operator": "Lt. Test"})       # police: not a fire talkgroup
    assert on31(rig, "4031-1-call_1-t0", T0 + 20, "", talker="60 Ctrl OP 3") == []
    got = rig.cad.incident_channel(iid, {"talkgroup": TG31, "operator": "Lt. Test"})
    assert [(c["tg"], c["source"], c["operator"]) for c in got["channels"]] == [(TG31, "manual", "Lt. Test")]
    assert rig.links(iid)["4031-1-call_1-t0"] == ("opchannel", "Fire/EMS 31")         # matched back to the dispatch
    assert rig.cad.full(iid)["channels"][0]["name"] == "Fire/EMS 31"
    rig.cad.incident_channel(iid, {"talkgroup": TG31, "remove": True, "operator": "Lt. Test"})
    assert chans(rig, iid) == [] and "4031-1-call_1-t0" not in rig.links(iid)
    rig.radio("307-1-call_1-t0", T0 + 30, "Engine 60, operate on Fire/EMS 31")
    assert chans(rig, iid) == []                                          # a rule never puts it back
    rig.cad.incident_channel(iid, {"talkgroup": TG31, "operator": "Lt. Test"})
    assert chans(rig, iid) == [(TG31, "manual")]                          # a person does


# -- speech-to-text --------------------------------------------------------------------------------------------------


def test_the_hub_sends_the_promoted_talkgroups_only_to_a_station_that_takes_them(tmp_path):
    from sizeup.cloud.hub import HubApp
    from tests.test_cloud import hub_cfg
    hub = HubApp(hub_cfg(tmp_path))
    try:
        hub.cad.source_rows = lambda: ROWS
        iid = hub.cad.create({"type": "STRU", "address": ADDR, "operator": "Lt. Test"})["id"]
        hub.cad.incident_channel(iid, {"talkgroup": TG31, "operator": "Lt. Test"})
        assert hub.promoted_keys() == {"tg:4031"}
        assert "promote" not in hub.channel_desired()["aws"]                 # a station from before: never sent
        hub.hub.status = {"channels": {"promote_ok": True}}
        want = hub.channel_desired()
        assert want["aws"]["promote"] == ["tg:4031"]
        since = hub.cad.store.one("SELECT since FROM incident_channels")["since"]
        assert want["aws"]["promote_since"] == {"tg:4031": since}             # the move: what the station raises from
        assert want["sig"] == channels.sig(want["prefs"], want["aws"]["ok"], want["tones"], want["aws"]["reserve"],
                                           want["squelch"], None, ["tg:4031"]) != channels.sig(
            want["prefs"], want["aws"]["ok"], want["tones"], want["aws"]["reserve"], want["squelch"])
        assert HubApp._whisper_fallback(type("J", (), {"kind": "traffic", "channel": "tg:4031"})(), hub.promoted_keys())
    finally:
        hub.stop()


def test_the_station_transcribes_a_promoted_talkgroup_like_ours_and_raises_what_waits(tmp_path):
    from sizeup.cloud import sttinput
    from tests.test_stt_input import offline_station
    st = offline_station(tmp_path)
    try:
        t = Traffic("4031-1-call_1-t0", "Fire/EMS 31", 0.0, 1.0, "", talkgroup=TG31)
        st.tg.main = lambda tg, system="": False                          # heard as context: LOW, shed when behind
        assert st._stt_class(t, "tg:4031") == LOW and st._stt_lane(t, "tg:4031") == sttinput.LOW
        assert st.stt_engine("tg:4031") == "local"
        raised = []
        st.stt.raise_channel = lambda key, prio, since: raised.append((key, prio, since)) or 1   # (SttQueue's own: below)
        word = {"ok": True, "why": "", "reserve": [], "promote": ["tg:4031"], "promote_since": {"tg:4031": 1000.0}}
        st.set_channel_prefs({}, word)
        assert st._stt_class(t, "tg:4031") == URGENT and st._stt_lane(t, "tg:4031") == sttinput.FIRE
        assert st.stt_engine("tg:4031") == "aws"                          # AWS can take it: like Fire/EMS 15
        assert raised == [("tg:4031", URGENT, 1000.0)]                    # what waits since the move goes with it
        assert App._late_prio(st._stt_class(t, "tg:4031"), True) == RERUN  # a late call: after every live one (#753)
        applied = st.channels_applied()
        assert applied["promote_ok"] is True
        # a restart keeps the hub's word: the same signature, and the hub's next push (the same) raises nothing again
        st._hub_aws = st._load_hub_aws()
        assert st._hub_aws["promote"] == ["tg:4031"] and st.channels_applied()["sig"] == applied["sig"]
        st.set_channel_prefs({}, word)
        assert raised == [("tg:4031", URGENT, 1000.0)]
        st.set_channel_prefs({}, {"ok": False, "why": "the monthly limit", "reserve": ["tg:307"], "promote": ["tg:4031"]})
        assert st.stt_engine("tg:4031") == "local" and st._stt_class(t, "tg:4031") == URGENT   # never the reserve
        st.set_channel_prefs({}, {"ok": True, "why": "", "reserve": []})                     # the tail over: demoted
        assert st._stt_class(t, "tg:4031") == LOW and st.stt_engine("tg:4031") == "local"
    finally:
        st.link.stop()


def test_raise_channel_raises_only_what_came_since_the_move_behind_fire_ems_15_never_a_late_call():
    def clip(name, start, late=False):
        t = Traffic(name, "x", start, start + 1.0, "")
        if late:
            t.late = True                                                  # (#747/#753: from while sizeup was down)
        return t
    q = SttQueue()
    for prio, item, ch in ((LOW, clip("old", 900.0), "tg:4031"), (URGENT, clip("fe15-a", 1100.0), "tg:307"),
                           (LOW, clip("a", 1050.0), "tg:4031"), (LIVE, clip("b", 1060.0), "tg:4031"),
                           (RERUN, clip("c", 1070.0), "tg:4031"), (LOW, clip("late", 1080.0, late=True), "tg:4031"),
                           (LOW, clip("d", 1090.0), "tg:4100"), (URGENT, clip("fe15-b", 1110.0), "tg:307")):
        q.put((prio, None, item, None, ch))
    assert q.raise_channel("tg:4031", URGENT, None) == 0                    # the move's time not known: nothing
    assert q.raise_channel("tg:4031", URGENT, 1000.0) == 2
    assert {e[2].id: e[0] for e in q._items} == {"old": LOW, "fe15-a": URGENT, "a": URGENT, "b": URGENT, "c": RERUN,
                                                 "late": LOW, "d": LOW, "fe15-b": URGENT}
    order = [q.get_nowait()[2].id for _ in range(4)]
    assert order == ["fe15-a", "fe15-b", "a", "b"]                          # behind Fire/EMS 15's that already waited


def test_a_stand_in_app_without_the_promoted_set_keeps_its_class():
    from sizeup.app import _promoted
    assert _promoted(object()) == frozenset() and App.promoted_keys


def test_a_folded_transmission_becomes_its_units_when_the_radio_is_named_and_leaves_when_it_is_anothers(rig):
    iid = call(rig)
    rig.cad.incident_channel(iid, {"talkgroup": TG31, "operator": "Lt. Test"})
    assert on31(rig, "4031-1-call_1-t0", T0 + 20, "") == [iid] and on31(rig, "4031-2-call_2-t0", T0 + 25, "") == [iid]
    with rig.cad.store.tx() as db:                                       # (the radio IDs heard, nobody's yet)
        db.execute("UPDATE radio_traffic SET radio_id = '1234567' WHERE id = '4031-1-call_1-t0'")
        db.execute("UPDATE radio_traffic SET radio_id = '7654321' WHERE id = '4031-2-call_2-t0'")
    rig.cad.comms.relink("p25", "1234567", "E60")                         # named: a unit on it
    rig.cad.comms.relink("p25", "7654321", "E191")                        # named: another department's
    assert rig.links(iid)["4031-1-call_1-t0"] == ("unit", "E60") and "4031-2-call_2-t0" not in rig.links(iid)
    assert on31(rig, "4031-3-call_3-t0", T0 + 30, "", rig_="E60") == [iid]
    assert rig.links(iid)["4031-3-call_3-t0"] == ("unit", "E60")


def test_promotion_ends_when_the_call_is_no_longer_heard_there(rig):
    from sizeup.cad.opchannel import QUIET_S
    iid = call(rig)
    rig.cad.incident_channel(iid, {"talkgroup": TG31, "operator": "Lt. Test"})
    added = rig.cad.store.one("SELECT at FROM incident_channels")["at"]
    assert rig.cad.opchan.promoted(added + QUIET_S - 1) == {TG31}
    assert rig.cad.opchan.promoted(added + QUIET_S + 1) == set()          # open for hours, nothing heard: not ours
    on31(rig, "4031-1-call_1-t0", added + QUIET_S + 5, "", talker="60 Ctrl OP 3")
    assert rig.cad.opchan.promoted(added + QUIET_S + 10) == {TG31}        # heard on it again
