"""Comms.on_context_text: another P25 system's transcripts (context only) to their own hook, after the write has
committed, and never to on_text's fire consumers (status hints, the radio log's link rules, possible-incoming, the
airport alert level, alarms). Synthetic data only."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from sizeup import sourcekeys
from sizeup.radio import systems

from tests.test_p25_systems import make_system
from tests.test_radios import Clock, cad, tx, uid

PD = 9001


def ctx(tid, start, text="zorbly flimmet quandle", tg=PD):
    """A transmission of a second P25 system ("metro"), as the app hands it to the CAD."""
    return dict(tx(tid, start, "Metro PD", unit="Car 7", tg=tg, text=text, radio_id="4100012"), system="metro",
                source_key=f"p25:metro:{tg}", group="Police", service="police", tier="text")


def ours(tid, start, text="engine sixty three on scene blorp"):
    return dict(tx(tid, start, "Fire/EMS 15", "E63", "E63", 307, text=text, radio_id="4242098"), ours=True,
                service="fire", group="Fire")


@pytest.fixture
def c(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    c.clockref = clock
    c.dbpath = str(tmp_path / "cad.sqlite")
    c.radiolog.inline = True
    return c


def spy(c):
    """Both hooks recorded, each call checked: outside any transaction, store.lock free, the row readable from a
    fresh connection (committed)."""
    calls = {"text": [], "context": []}
    inner = c.comms.on_text

    def check(tid):
        assert not c.store.db.in_transaction
        free = []
        th = threading.Thread(target=lambda: free.append(c.store.lock.acquire(blocking=False) and
                                                         (c.store.lock.release() or True)))
        th.start()
        th.join()
        assert free == [True]                                     # no one holds store.lock while the hook runs
        with sqlite3.connect(c.dbpath) as fresh:
            return fresh.execute("SELECT text FROM radio_traffic WHERE id = ?", (tid,)).fetchone()

    def on_text(tid):
        calls["text"].append((tid, check(tid)))
        if inner is not None:
            inner(tid)

    def on_context(tid):
        calls["context"].append((tid, check(tid)))

    c.comms.on_text, c.comms.on_context_text = on_text, on_context
    return calls


def test_a_context_row_with_text_fires_on_context_text_once_after_commit(c):
    calls = spy(c)
    c.record_traffic(ctx("metro-1", c.clockref.t - 5, "glorp vantic fizzle"))
    assert calls["context"] == [("metro-1", ("glorp vantic fizzle",))]
    assert calls["text"] == []


def test_a_context_row_without_text_fires_nothing(c):
    calls = spy(c)
    c.record_traffic(ctx("metro-2", c.clockref.t - 5, ""))
    assert calls == {"text": [], "context": []}


def test_our_row_fires_on_text_only(c):
    calls = spy(c)
    c.record_traffic(ours("307-1-call_1-t0", c.clockref.t - 5))
    assert [t for t, _ in calls["text"]] == ["307-1-call_1-t0"]
    assert calls["context"] == []


def test_retext_of_a_context_row_fires_on_context_text_and_of_ours_on_text(c):
    c.record_traffic(ctx("metro-3", c.clockref.t - 5))
    c.record_traffic(ours("307-2-call_1-t0", c.clockref.t - 4))
    calls = spy(c)
    assert c.comms.retext("metro-3", "plonk wibbet", "done", "m")
    assert calls == {"text": [], "context": [("metro-3", ("plonk wibbet",))]}
    assert c.comms.retext("307-2-call_1-t0", "engine sixty three blorp again", "done", "m")
    assert calls["context"] == [("metro-3", ("plonk wibbet",))]
    assert calls["text"] == [("307-2-call_1-t0", ("engine sixty three blorp again",))]
    assert not c.comms.retext("no-such-row", "x", "done")                  # nothing written: nothing fires
    assert len(calls["text"]) == 1 and len(calls["context"]) == 1


def test_set_alt_and_rerun_done_change_no_text_and_fire_neither(c):
    c.record_traffic(ctx("metro-4", c.clockref.t - 5))
    calls = spy(c)
    assert c.comms.set_alt("metro-4", {"text": "other engine snerk", "model": "m"})
    assert c.comms.rerun_done("metro-4", "same")
    assert calls == {"text": [], "context": []}


def test_a_raising_hook_never_breaks_record_or_retext(c):
    seen, logged = [], []
    c.comms.log = logged.append

    def boom(tid):
        seen.append(c.store.db.in_transaction)
        raise RuntimeError("kaput")

    c.comms.on_context_text = boom
    assert c.record_traffic(ctx("metro-5", c.clockref.t - 5)) == []
    assert c.comms.retext("metro-5", "flumph", "done")
    assert seen == [False, False]                                          # never inside the transaction
    assert c.store.one("SELECT text FROM radio_traffic WHERE id = ?", ("metro-5",))["text"] == "flumph"
    assert len(logged) == 2 and all("RuntimeError" in m for m in logged)
    assert not c.store.db.in_transaction
    c.record_traffic(ctx("metro-6", c.clockref.t - 4))                     # the store is still usable
    assert c.store.one("SELECT id FROM radio_traffic WHERE id = ?", ("metro-6",))


def test_context_rows_never_reach_a_fire_consumer(c):
    """Every consumer on_text feeds (Cad._on_text) spied on: record and retext several context rows (on a talkgroup
    number we use as well, with words the consumers would act on) and none of them sees a context id. (The alarms
    are fed by the app, not by on_text: sizeup/app.py _alarm, which drops another system's rows itself.)"""
    seen: list[tuple[str, object]] = []

    def wrap(obj, name, key=lambda a: a[0]):
        f = getattr(obj, name)

        def w(*a, **kw):
            seen.append((name, key(a)))
            return f(*a, **kw)
        setattr(obj, name, w)

    wrap(c.hints, "consider")
    wrap(c.radiolog, "consider")
    wrap(c.radiolog, "matches", lambda a: a[0]["id"])
    wrap(c.radiolog, "_link")
    wrap(c.sources, "consider_radio")
    wrap(c.airport_watch, "heard_radio", lambda a: a[0]["id"])
    wrap(c.auto, "on_hints", lambda a: [h.get("traffic_id") for h in a[0] or []])
    got = []
    c.comms.on_context_text = got.append
    t = c.clockref.t
    words = "mayday mayday engine sixty three on scene blorpville alert level two"
    ids = [f"metro-{k}" for k in range(5)]
    for k, tid in enumerate(ids):
        c.record_traffic(ctx(tid, t - 20 + k, words, tg=(PD, 307, 333)[k % 3]))
    for tid in ids:
        c.comms.retext(tid, words + " again", "done", "m")
    c.record_traffic(ours("307-3-call_1-t0", t - 1))                      # the spies do see our rows
    assert got == ids + ids
    assert all("metro-" not in str(k) for _, k in seen), seen
    assert ("consider", "307-3-call_1-t0") in seen


def _copy_as_context(c, tid, new):
    """Our row again, byte for byte, but on another P25 system ("metro"): only system and source differ."""
    row = dict(c.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,)))
    row.update(id=new, system="metro", source=f"p25:metro:{row['talkgroup']}")
    cols = list(row)
    with c.store.tx() as db:
        db.execute(f"INSERT INTO radio_traffic ({', '.join(f'[{k}]' for k in cols)}) VALUES ({', '.join('?' * len(cols))})",
                   [row[k] for k in cols])
    return c.store.one("SELECT * FROM radio_traffic WHERE id = ?", (new,))


def test_the_consumers_own_guards_still_hold_for_a_context_row(c):
    """on_text never brings them a context row now; their is_context guards stay as a second line. Called directly,
    each consumer acts on our row (the positive control) and on nothing for the identical row of another system."""
    inc = c.create({"type": "STRU", "address": "17 Quillfeather Ln", "operator": "op"})
    c.dispatch(inc["id"], {"unit_ids": [uid(c, "E62")], "operator": "op"})
    c.clockref.t += 30                                       # heard after the dispatch
    t = c.clockref.t
    c.comms.on_text = None                                   # the consumers run only when this test calls them
    c.record_traffic(ours("307-1-call_1-t0", t, "Engine 62 on location") | {"rig": "E62", "unit": "E62"})
    c.record_traffic(dict(tx("307-2-call_2-t0", t + 1, "Fire/EMS 15", unit="60 Ctrl OP 6", tg=307,
                             text="Engine 64, 60 Control"), ours=True, service="fire", group="Fire"))
    # said: E62 (on the call) on location; called: 60 Control opening a call to E64 (not on one)
    said, called = (c.store.one("SELECT * FROM radio_traffic WHERE id = ?", (x,))
                    for x in ("307-1-call_1-t0", "307-2-call_2-t0"))
    ctx_said, ctx_called = _copy_as_context(c, said["id"], "metro-g1"), _copy_as_context(c, called["id"], "metro-g2")
    # the rows of another system first (nothing of ours made yet that could hide what they would make): nothing
    assert c.hints.consider(ctx_said["id"]) == []
    assert c.sources.consider_radio(ctx_called["id"]) is None
    assert c.radiolog.matches(ctx_said) == {} and c.radiolog.consider(ctx_said["id"]) == []
    assert c.store.q("SELECT * FROM status_hints") == []
    # the positive control: the same rows of ours
    assert [h["unit"] for h in c.hints.consider(said["id"])] == ["E62"]
    assert c.sources.consider_radio(called["id"]) is not None
    assert inc["id"] in c.radiolog.matches(said)
    assert [r["traffic_id"] for r in c.store.q("SELECT traffic_id FROM status_hints")] == [said["id"]]
    assert [r["ref"] for r in c.store.q("SELECT ref FROM incoming")] == [called["id"]]
    assert c.store.q("SELECT * FROM radio_links WHERE traffic_id LIKE 'metro-%'") == []


@pytest.mark.parametrize("row", [
    dict(tx("fg1-1", 0, "FG1", radio_id="4100031")),                                   # analog FM
    dict(tx("twr-1", 0, "Tower"), mhz=119.7),                                          # airband
    dict(tx("fm-metro-1", 0, "Metro FM", radio_id="4100032"), system="metro"),         # a system, no talkgroup
])
def test_retext_of_a_row_with_no_talkgroup_goes_to_on_text_only(c, row):
    c.record_traffic({**row, "start": c.clockref.t - 5, "end": c.clockref.t - 3})
    calls = spy(c)
    assert c.comms.retext(row["id"], "snorkel wimble", "done", "m")
    assert [tid for tid, _ in calls["text"]] == [row["id"]] and calls["context"] == []


@pytest.mark.parametrize("system", ["metro", "analog", "airband", sourcekeys.COUNTY, ""])
def test_record_and_retext_route_a_p25_row_the_same_way(c, system):
    """One rule (sourcekeys.other_p25) for both: whatever the system is called, record and retext agree."""
    calls = spy(c)
    row = dict(ctx("p25-x", c.clockref.t - 5, "quibble zent"), system=system,
               source_key=f"p25:{system}:{PD}" if system else "")
    c.record_traffic(row)
    c.comms.retext("p25-x", "quibble zent again", "done", "m")
    other = system not in ("", sourcekeys.COUNTY)
    hook = "context" if other else "text"
    assert [tid for tid, _ in calls[hook]] == ["p25-x", "p25-x"]
    assert calls["text" if other else "context"] == []
    assert sourcekeys.other_p25(row) is other


def test_other_p25_agrees_with_is_context_for_every_name_a_system_may_have():
    for name in ("metro", "mrrs", "east", sourcekeys.COUNTY, ""):
        r = {"talkgroup": PD, "system": name}
        assert sourcekeys.other_p25(r) == sourcekeys.is_context(r)
    assert not sourcekeys.other_p25({"talkgroup": None, "system": "metro"})
    assert not sourcekeys.other_p25({})
    assert set(sourcekeys.RESERVED_SYSTEMS) == {sourcekeys.COUNTY, "analog", "airband"}


@pytest.mark.parametrize("name", ["analog", "airband", sourcekeys.COUNTY])
def test_a_system_named_like_one_of_ours_is_refused(tmp_path, name):
    make_system(tmp_path, name, tgs="9101,238d,Metro Ops,T,d,Police,X,1,text\n", jobs=("Zz 1",))
    make_system(tmp_path, "metro", tgs="9102,238e,Metro PD,T,d,Police,X,1,text\n", jobs=("Zz 2",))
    problems: list[str] = []
    assert [s.name for s in systems.discover(tmp_path, problems=problems)] == ["metro"]
    assert len(problems) == 1 and f"system name {name!r} is reserved" in problems[0]
    with pytest.raises(systems.SystemError_, match="is reserved"):
        systems.discover(tmp_path)
