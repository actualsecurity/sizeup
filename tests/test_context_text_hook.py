"""Comms.on_context_text: another P25 system's transcripts (context only) to their own hook, after the write has
committed, and never to on_text's fire consumers (status hints, the radio log's link rules, possible-incoming, the
airport alert level, alarms). Synthetic data only."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from tests.test_radios import Clock, cad, tx

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
    """Every consumer on_text feeds, and the alarms, spied on: record and retext several context rows (on a
    talkgroup number we use as well, with words the consumers would act on) and none of them sees a context id."""
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
    wrap(c.alarms, "on_traffic", lambda a: a[0]["id"])
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
