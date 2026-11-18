"""A transmission the console's state doesn't show rebuilds nothing (native-notes FINDINGS §1.6, §6.2 d).

Every stored transmission is a transaction that moves store.rev, and Cad.state() was rebuilt (run cards, incidents,
radio logs, status-word repair, units) for each one, including other systems' talkgroups. Now Cad.state() is keyed
on store.state_rev, which such a transmission leaves alone: one on no incident (before or after, by any rule, to it or
to another turn of its call) and not in the 60 Control feed (its talkgroups, the paging channel). store.rev, comms.rev
and the state's comms_rev still move, so the Radio page and every per-rev cache see it.

The safety net: after every write of a mixed run, the state served (cached or not) equals one built from scratch.

No radios, no network. Every transmission, unit, talkgroup number and address here is invented."""

from __future__ import annotations

import json
import random
import time

from sizeup.cad.comms import Comms
from tests.test_radios import Clock, _event_with, cad, tx, uid


def make(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    c.radiolog.inline = True                  # (the units backfill on the test's thread: no write lands mid-test)
    c.comms.context_broadcast = True          # the hub behind Access: the state's comms_rev is the full one
    c.clockref = clock
    return c


def other(tid, start, tg=9001, text="words"):
    """Another P25 system's row (context)."""
    return dict(tx(tid, start, "PD Dispatch", unit="Car 7", tg=tg, text=text), system="metro", source_key=f"p25:metro:{tg}",
                group="Police", service="police", tier="text")


def fresh(c) -> dict:
    """The state built from scratch, now."""
    c._state = None
    return c.state()


def same(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_a_transmission_on_no_incident_moves_rev_and_comms_rev_but_rebuilds_nothing(tmp_path):
    c = make(tmp_path)
    inc = _event_with(c, ["E63"], c.clockref)
    t = c.clockref.t + 5
    st = c.state()
    rev, srev, crev = c.store.rev, c.store.state_rev, st["comms_rev"]
    for row in (other("pd-1", t), tx("fg-1", t + 1, text="an unnamed radio"), tx("tw-1", t + 2, "Tower", text="made-up words"),
                tx("eu-1", t + 3, "Ops 2", tg=8123, unit="Car 3", text="not ours")):
        assert c.record_traffic(row) == []
        again = c.state()
        assert c.store.rev > rev and c.store.state_rev == srev                     # rev moves; the state's doesn't
        assert again["incidents"] is st["incidents"] and again["units"] is st["units"] and again["feed"] is st["feed"]
        assert again["comms_rev"] != crev and again["rev"] == c.store.rev          # the Radio page still asks again
        rev, crev = c.store.rev, again["comms_rev"]
        assert same(again, fresh(c))
        st = c.state()
    assert inc["id"] in {i["id"] for i in st["incidents"]}


def test_a_transmission_on_an_incident_rebuilds_it_and_shows_on_its_radio_log(tmp_path):
    c = make(tmp_path)
    inc = _event_with(c, ["E63"], c.clockref)
    c.clockref.t += 10
    st = c.state()
    srev = c.store.state_rev
    assert c.record_traffic(tx("fg-63", c.clockref.t, rig="E63", radio_id="6301", text="engine 63 on location")) == [inc["id"]]
    assert c.store.state_rev > srev
    now = c.state()
    assert now["incidents"] is not st["incidents"]
    radio = next(i for i in now["incidents"] if i["id"] == inc["id"])["radio"]
    assert [r["id"] for r in radio] == ["fg-63"] and radio[0]["text"] == "engine 63 on location"
    # its transcript again (the hub's own speech-to-text after the station's): still on it, so rebuilt again
    srev = c.store.state_rev
    c.record_traffic(tx("fg-63", c.clockref.t, rig="E63", radio_id="6301", text="engine 63 on location, investigating"))
    assert c.store.state_rev > srev
    radio = next(i for i in c.state()["incidents"] if i["id"] == inc["id"])["radio"]
    assert radio[0]["text"] == "engine 63 on location, investigating"


def test_a_row_of_the_feed_or_a_page_rebuilds(tmp_path):
    c = make(tmp_path)
    t = c.clockref.t
    tg = c.feed.cfg.talkgroups[0]
    c.state()
    for row in (tx("307-a", t + 1, "Fire Dispatch", tg=tg, unit="60 Ctrl", text="made-up dispatch words"),
                dict(tx("pg-1", t + 2, c.feed.paging[0], text="a made-up page"),
                     tones=[{"a_hz": 1000.0, "b_hz": 2000.0, "names": [], "ours": False}])):
        srev = c.store.state_rev
        c.record_traffic(row)
        assert c.store.state_rev > srev, row["id"]
        assert row["id"] in {r["id"] for r in c.state()["feed"]["rows"]}


def test_a_row_moved_out_of_the_feed_or_off_an_incident_rebuilds(tmp_path):
    c = make(tmp_path)
    t = c.clockref.t
    tg = c.feed.cfg.talkgroups[0]
    c.record_traffic(tx("x-1", t + 1, "Fire Dispatch", tg=tg, unit="60 Ctrl"))
    c.state()
    srev = c.store.state_rev
    c.record_traffic(tx("x-1", t + 1, "Ops 2", tg=8123, unit="60 Ctrl"))           # recorded again, on another talkgroup
    assert c.store.state_rev > srev and "x-1" not in {r["id"] for r in c.state()["feed"]["rows"]}
    # a person's own link: the row is on that incident; recorded again it still rebuilds (its text is on the incident)
    inc = c.create({"type": "ALARM", "address": "1 Example Ave", "operator": "op"})
    c.record_traffic(tx("y-1", t + 3, "Ops 2", tg=8123, unit="Car 3"))
    c.comms.link("y-1", inc["id"], "op")
    c.state()
    srev = c.store.state_rev
    c.record_traffic(tx("y-1", t + 3, "Ops 2", tg=8123, unit="Car 3", text="new words"))
    assert c.store.state_rev > srev
    assert next(i for i in c.state()["incidents"] if i["id"] == inc["id"])["radio"][0]["text"] == "new words"


def test_any_direct_rev_bump_moves_the_state_rev_too(tmp_path):
    c = make(tmp_path)
    st = c.state()
    srev = c.store.state_rev
    c.store.rev += 1                         # what a module does to have the state built again (settings, preplan ...)
    assert c.store.state_rev == srev + 1 and c.state()["incidents"] is not st["incidents"]


def test_without_the_cads_hook_every_transmission_rebuilds(tmp_path):
    c = make(tmp_path)
    comms = Comms(c.store)                   # in_state None: a Comms on its own never calls a write quiet
    srev = c.store.state_rev
    comms.record(other("pd-9", c.clockref.t))
    assert c.store.state_rev > srev


def test_a_quiet_mark_never_outlives_its_transaction(tmp_path):
    c = make(tmp_path)
    with c.store.tx():
        c.store.quiet_for_state()            # marked, nothing written: nothing moves
    srev, rev = c.store.state_rev, c.store.rev
    with c.store.tx() as db:                 # the next transaction writes: the state's rev moves as always
        db.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES ('x', 'k', '1', 0)")
    assert c.store.rev == rev + 1 and c.store.state_rev == srev + 1
    try:
        with c.store.tx() as db:
            c.store.quiet_for_state()
            db.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES ('x', 'k2', '1', 0)")
            raise RuntimeError("rolled back")
    except RuntimeError:
        pass
    with c.store.tx() as db:
        db.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES ('x', 'k3', '1', 0)")
    assert c.store.state_rev == srev + 2


def test_after_every_write_of_a_mixed_run_the_state_is_what_a_fresh_build_makes(tmp_path):
    """The safety net: whatever is recorded (ours, other systems', the feed, pages, rows on and off incidents, rows
    recorded again, units moving, incidents opened and closed), the state served is never stale."""
    c = make(tmp_path)
    rng = random.Random(415)
    clock = c.clockref
    tg = c.feed.cfg.talkgroups[0]
    incs = [_event_with(c, ["E63"], clock)["id"]]
    quiet = rebuilt = 0
    for i in range(160):
        clock.t += rng.choice((1, 2, 5))
        roll = rng.random()
        tid = f"r{i}" if rng.random() > 0.2 or i == 0 else f"r{rng.randrange(i)}"   # sometimes a row recorded again
        srev = c.store.state_rev
        if roll < 0.3:
            c.record_traffic(other(tid, clock.t, tg=rng.choice((9001, 9002)), text=f"made-up words {i}"))
        elif roll < 0.45:
            c.record_traffic(tx(tid, clock.t, "FG1", rig=rng.choice(("E63", "E60", "")), radio_id=rng.choice(("6301", "")),
                                text=f"engine words {i}"))
        elif roll < 0.55:
            c.record_traffic(tx(tid, clock.t, "Fire Dispatch", tg=tg, unit=rng.choice(("60 Ctrl", "E63")), text=f"dispatch {i}"))
        elif roll < 0.65:
            c.record_traffic(tx(tid, clock.t, "Ops 2", tg=8123, unit="Car 3", text=f"other words {i}"))
        elif roll < 0.72:
            c.record_traffic(tx(tid, clock.t, "Tower", text=f"tower words {i}"))
        elif roll < 0.8:
            c.record_traffic(dict(tx(tid, clock.t, c.feed.paging[0], text=f"page {i}"),
                                  tones=[{"a_hz": 1000.0, "b_hz": 2000.0, "names": [], "ours": False}]))
        elif roll < 0.86:
            incs.append(_event_with(c, [rng.choice(("E60", "E64", "L31"))], clock)["id"])
        elif roll < 0.9 and len(incs) > 1:
            c.close(incs.pop(0), {"disposition": "COMPLETE", "operator": "op"})
        elif roll < 0.95:
            c.state()                        # a push between writes
            continue
        quiet += c.store.state_rev == srev
        rebuilt += c.store.state_rev != srev
        for _ in range(100):                 # (a dispatch's voice is written by its own thread: then look again)
            at = c.store.state_rev
            served, built = c.state(), fresh(c)
            if c.store.state_rev == at:
                break
            time.sleep(0.01)
        assert same(served, built), f"stale after step {i}"
    assert quiet > 20 and rebuilt > 20, (quiet, rebuilt)       # both kinds were exercised
