"""The Radio page's change log (GitHub #415, ?changed=) never stands in the way of the radio log.

The log (radio_changes) is written by the application after a write has committed, as its own best-effort statement
(Comms.touch inside the write's transaction, Comms.flush_changes after it): no trigger, no retry. So here:

- every function that writes the radio log, its links, a kept mark or an alarm notes the change (a new write site
  without it fails), and every public method of Comms is either walked as a writer or named as a reader;
- with the log dropped, refusing every entry, or its writer raising, every writer still stores its data, raises
  nothing, and runs each of its statements exactly once; the log says so once and readers are told to ask afresh;
- a write that is rolled back leaves no entry; an entry for a row that isn't there is harmless;
- a reader who may not see another P25 system's traffic gets a cursor and a rev that don't move with it.

No radios, no network. Every transmission, unit, talkgroup number and address here is invented."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from sizeup.cad.api import CadApi
from sizeup.cad.comms import Comms
from sizeup.cloud.access import Identity
from tests.test_radios import Clock, _event_with, cad, tx

ROOT = Path(__file__).resolve().parent.parent
WRITE = re.compile(r"(INSERT( OR [A-Z]+)? INTO|UPDATE|DELETE FROM) (radio_traffic|radio_links|radio_link_kept|alarms)\b")
# Writes that need no note: what runs when the database is opened, before any page holds a cursor (every cursor from
# before a start is told to reload: Comms.changes_floor)
AT_OPEN = {("store.py", "_operator_identity"),      # the schema migrations (the def above them in the file)
           ("alarms.py", "__init__")}               # alarms past their retention, dropped at the start


def other(tid, start, tg=9001, text="words"):
    return dict(tx(tid, start, "PD Dispatch", unit="Car 7", tg=tg, text=text), system="metro", source_key=f"p25:metro:{tg}",
                group="Police", service="police", tier="text")


def make(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock, airband=("Tower",))
    c.clockref = clock
    c.comms.context_broadcast = True
    c.said = []
    c.comms.log = c.said.append
    t = clock.t
    c.inc = _event_with(c, ["E63"], clock)
    c.record_traffic(tx("fg-1", t + 1, rig="E63", radio_id="6301", text="engine 63 on location"))
    c.record_traffic(tx("fg-2", t + 2, text="a made-up key-up"))
    c.record_traffic(dict(tx("pg-1", t + 3, "South 2", text="a made-up page"), tones=[{"a_hz": 1000.0, "b_hz": 2000.0, "names": [], "ours": False}]))
    c.record_traffic(dict(tx("tw-1", t + 4, "Tower", text="made-up tower words")))          # an airband row logged as analog
    c.record_traffic(tx("fg-x", t + 5, radio_id="0999", text="an unnamed radio"))
    c.record_traffic(other("pd-1", t + 6))
    c.alarm = c.alarms.on_traffic(dict(tx("fg-m", t + 7, text="mayday mayday mayday", rig="E63", radio_id="6301"), at=t + 7))
    c.record_traffic(tx("fg-m", t + 7, text="mayday mayday mayday", rig="E63", radio_id="6301"))
    return c


def top(c):
    try:
        return c.store.db.execute("SELECT MAX(seq) FROM radio_changes").fetchone()[0] or 0
    except sqlite3.Error:
        return 0


def since(c, seq):
    return [r[0] for r in c.store.db.execute("SELECT traffic_id FROM radio_changes WHERE seq > ? ORDER BY seq", (seq,))]


def row(c, tid):
    return c.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))


# Every writer: (name, what it does, the id its change is noted under ("reset": every page asks afresh), what must
# be stored afterwards). In this order on one CAD.
def writers(c):
    t, iid = c.clockref.t, c.inc["id"]
    links = lambda tid: {r["incident_id"] for r in c.store.q("SELECT incident_id FROM radio_links WHERE traffic_id = ?", (tid,))}  # noqa: E731
    return [
        ("record", lambda: c.comms.record(tx("fg-new", t + 20, rig="E63", radio_id="6301", text="a new fire row")), "fg-new",
         lambda: row(c, "fg-new")["text"] == "a new fire row" and iid in links("fg-new")),
        ("record again", lambda: c.comms.record(tx("fg-new", t + 20, rig="E63", radio_id="6301", text="the same row, its words in")),
         "fg-new", lambda: row(c, "fg-new")["text"] == "the same row, its words in"),
        ("record context", lambda: c.comms.record(other("pd-new", t + 21)), "pd-new", lambda: row(c, "pd-new")["system"] == "metro"),
        ("retext", lambda: c.comms.retext("fg-2", "its transcript", "done", alt={"text": "another", "model": "m"}), "fg-2",
         lambda: row(c, "fg-2")["text"] == "its transcript"),
        ("set_alt", lambda: c.comms.set_alt("fg-2", {"text": "a third reading", "model": "m"}), "fg-2",
         lambda: row(c, "fg-2")["alt_text"] == "a third reading"),
        ("rerun_done", lambda: c.comms.rerun_done("fg-2", "same"), "fg-2", lambda: row(c, "fg-2")["rerun_result"] == "same"),
        ("link", lambda: c.comms.link("fg-2", iid, "Lt. Sample"), "fg-2", lambda: iid in links("fg-2")),
        ("keep", lambda: c.comms.link("pd-1", iid, "Lt. Sample") and None, "pd-1", lambda: iid in links("pd-1")),
        ("unlink", lambda: c.comms.unlink("fg-2", iid, "Lt. Sample"), "fg-2", lambda: iid not in links("fg-2")),
        ("link_pages", lambda: c.comms.link_pages(iid, ["pg-1"]), "pg-1", lambda: iid in links("pg-1")),
        ("relink", lambda: c.comms.relink("mdc", "0999", "E63"), "fg-x", lambda: row(c, "fg-x")["rig"] == "E63"),
        ("retag_pages", lambda: c.comms.retag_pages(lambda a, b: ["Sample FD"], lambda names: True), "pg-1",
         lambda: "Sample FD" in row(c, "pg-1")["tones"]),
        ("fix_air", lambda: c.comms.fix_air(["Tower"]), "reset", lambda: row(c, "tw-1")["system"] == "airband"),
        ("alarm raised", lambda: c.alarms.on_traffic(dict(tx("fg-new", t + 20, text="mayday mayday", rig="E63", radio_id="6301"), at=t + 20)),
         "fg-new", lambda: c.store.one("SELECT 1 AS x FROM alarms WHERE traffic_id = 'fg-new'") is not None),
        ("alarm acknowledged", lambda: c.alarms.acknowledge(c.alarm["id"], {"operator": "Lt. Sample"}), "fg-m",
         lambda: c.store.one("SELECT ack_at FROM alarms WHERE id = ?", (c.alarm["id"],))["ack_at"]),
        ("alarm cleared", lambda: c.alarms.all_clear(c.alarm["id"], {"operator": "Lt. Sample"}), "fg-m",
         lambda: c.store.one("SELECT clear_at FROM alarms WHERE id = ?", (c.alarm["id"],))["clear_at"]),
        ("link kept", lambda: (c.store.db.execute("UPDATE radio_links SET why = 'mention' WHERE traffic_id = 'pg-1'"),
                               c.radiolog.keep("pg-1", iid, "Lt. Sample"))[1], "pg-1",
         lambda: c.store.one("SELECT 1 AS x FROM radio_link_kept WHERE traffic_id = 'pg-1'") is not None),
        ("link no longer kept", lambda: c.radiolog.keep("pg-1", iid, "Lt. Sample", undo=True), "pg-1",
         lambda: c.store.one("SELECT 1 AS x FROM radio_link_kept WHERE traffic_id = 'pg-1'") is None),
        ("mention links narrowed", lambda: c.radiolog.narrow(iid), "reset", lambda: True),
        ("a unit's mention links removed", lambda: c.radiolog.unit_removed(iid, "E63"), "reset", lambda: True),
        ("note", lambda: c.note(iid, {"text": "E63 reports a made-up condition", "operator": "Lt. Sample", "units": ["E63"]}), "note-new",
         lambda: c.store.one("SELECT 1 AS x FROM events WHERE kind = 'note' AND text LIKE 'E63 reports%'") is not None),
        ("prune", lambda: (setattr(c.clockref, "t", c.clockref.t + 91 * 24 * 3600), c.comms.prune())[1], "fg-2",
         lambda: row(c, "fg-2") is None and row(c, "fg-new") is not None),         # fg-new is on the event: kept
    ]


WRITERS = {"record", "retext", "set_alt", "rerun_done", "link", "unlink", "link_pages", "relink", "retag_pages", "fix_air", "prune"}
# Public methods of Comms that write nothing a row of the Radio page shows (activity: the per-minute counts)
READERS = {"activity", "activity_rows", "audio_per_day", "context_clips", "emergencies", "events_of", "facets", "for_incidents",
           "id_stats", "kept_for_events", "kept_for_tier", "minute_counts", "page", "query", "source_names",
           "rev_for", "sources_seen", "split", "talkgroups"}
NOTES = {"touch", "touch_reset", "note_change", "flush_changes"}               # the change notes themselves


def test_every_function_that_writes_the_radio_log_notes_the_change():
    """A new write site without a note fails here: by the source (every function with a write statement on these
    tables touches), and by Comms's own surface (every public method is a writer walked below, or a named reader)."""
    missing = []
    for f in sorted((ROOT / "sizeup").rglob("*.py")):
        lines = f.read_text().split("\n")
        for i, line in enumerate(lines):
            if not WRITE.search(line):
                continue
            k = i
            while not re.match(r"\s*def \w+", lines[k]):
                k -= 1
            name = re.match(r"\s*def (\w+)", lines[k]).group(1)
            indent = len(lines[k]) - len(lines[k].lstrip())
            j = k + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
                j += 1
            if "touch" not in "\n".join(lines[k:j]) and (f.name, name) not in AT_OPEN:
                missing.append(f"{f.name}:{i + 1} {name}()")
    assert not missing, "writes the radio log without Comms.touch (the Radio page would show it late): " + ", ".join(missing)
    public = {n for n in dir(Comms) if not n.startswith("_") and callable(getattr(Comms, n))}
    assert public == WRITERS | READERS | NOTES, f"new on Comms, say what it is: {sorted(public ^ (WRITERS | READERS | NOTES))}"


def test_every_writer_leaves_a_change_entry(tmp_path):
    c = make(tmp_path)
    walked = set()
    for name, do, noted, stored in writers(c):
        before = top(c)
        do()
        got = since(c, before)
        assert noted in got, f"{name}: no change entry for {noted} (got {got})"
        assert stored(), name
        walked.add(name.split()[0])
    assert WRITERS <= walked
    # each entry carries its row's system, read after the write committed (also a new row's, and a pruned one's)
    systems = dict(c.store.db.execute("SELECT traffic_id, system FROM radio_changes WHERE traffic_id IN ('fg-new', 'pd-new', 'fg-2') "
                                      "ORDER BY seq").fetchall())
    assert systems == {"fg-new": "analog", "pd-new": "metro", "fg-2": "analog"}
    assert c.store.db.execute("SELECT gone FROM radio_changes WHERE traffic_id = 'fg-2' ORDER BY seq DESC LIMIT 1").fetchone()[0] == 1
    assert not c.said and not c.comms.changes_broken
    assert not c.store.q("SELECT name FROM sqlite_master WHERE type = 'trigger'")       # nothing hangs on the tables


def trace(c):
    """Every write statement run from here on, as SQLite ran it (values and all), but the change log's own."""
    seen = []
    c.store.db.set_trace_callback(lambda sql: seen.append(sql))
    return lambda: Counter(s for s in seen if "radio_changes" not in s and s.split()[0].upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE"))


def refuse(c):
    c.store.db.execute("CREATE TRIGGER no_changes BEFORE INSERT ON radio_changes BEGIN SELECT RAISE(ABORT, 'database is locked'); END")


def raising(c):
    def boom():
        raise sqlite3.OperationalError("database is locked")
    c.store.after_commit = boom


@pytest.mark.parametrize("how", ["dropped", "refusing", "raising"])
def test_a_broken_change_log_never_costs_a_write_and_nothing_runs_twice(tmp_path, how):
    whole = make(tmp_path / "whole")
    count = trace(whole)
    for _name, do, _noted, _stored in writers(whole):
        do()
    healthy = count()

    c = make(tmp_path / "broken")
    cursor = c.comms_query({"view": ["ours"]})["cursor"]
    {"dropped": lambda c: c.store.db.execute("DROP TABLE radio_changes"), "refusing": refuse, "raising": raising}[how](c)
    count = trace(c)
    for name, do, _noted, stored in writers(c):
        do()                                                    # raises nothing
        assert stored(), f"{how}: {name} did not store its data"
    assert count() == healthy, f"{how}: a write ran more or fewer times than with the log whole"
    assert len(healthy) > 40                                    # (and the comparison is of something)
    if how != "raising":                                        # (a raising hook is the store's to swallow: the log never knew)
        assert c.comms.changes_broken and len(c.said) == 1 and "change log is off" in c.said[0]
        assert c.comms_query({"view": ["ours"], "changed": [str(cursor)]})["reload"] is True     # readers ask afresh
        first = c.comms_query({"view": ["ours"]})
        assert any(r["id"] == "fg-new" for r in first["rows"])  # and the page itself is whole
        # the log back (here: made again), and its minute over: written again, and what was missed is a reload
        c.store.db.execute("DROP TRIGGER IF EXISTS no_changes")
        c.store.db.executescript("CREATE TABLE IF NOT EXISTS radio_changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, traffic_id TEXT "
                                 "NOT NULL, system TEXT, gone INTEGER NOT NULL DEFAULT 0);")
        c.comms._retry_at = 0.0
        c.comms.retext("fg-1", "written again", "done")
        assert not c.comms.changes_broken and since(c, 0)[-1] == "fg-1"
        assert c.comms_query({"view": ["ours"], "changed": [str(cursor)]})["reload"] is True
        now = c.comms_query({"view": ["ours"]})["cursor"]
        c.comms.retext("fg-1", "and told", "done")
        told = c.comms_query({"view": ["ours"], "changed": [str(now)]})
        assert told["reload"] is False and [r["text"] for r in told["rows"]] == ["and told"]


def test_a_rolled_back_write_leaves_no_entry_and_a_stray_entry_is_harmless(tmp_path):
    c = make(tmp_path)
    before = top(c)

    def boom(*a, **k):
        raise RuntimeError("made-up failure inside the write")
    real = c.comms._count
    c.comms._count = boom                                       # record() fails after its INSERT, inside its transaction
    with pytest.raises(RuntimeError):
        c.comms.record(tx("fg-lost", c.clockref.t + 30, text="never stored"))
    c.comms._count = real
    assert row(c, "fg-lost") is None and since(c, before) == [] and c.comms._pending == []
    c.comms.record(tx("fg-next", c.clockref.t + 31))             # the next write: only its own entry
    assert since(c, before) == ["fg-next"]
    # an entry for a row that isn't there (a writer noted, then its row went): a signed-in page drops the id if it has
    # it; a reader who may not see everything is told nothing (its system isn't known)
    api, who = CadApi(c), Identity(email="someone@example.org")
    cur = json.loads(api.get("/api/cad/comms", "view=all", who)[1])["cursor"]
    c.comms.note_change("no-such-row")
    signed = json.loads(api.get("/api/cad/comms", f"view=all&changed={cur}", who)[1])
    assert signed["rows"] == [] and signed["gone"] == ["no-such-row"] and signed["reload"] is False
    anon_cur = json.loads(api.get("/api/cad/comms", "view=all", None)[1])["cursor"]
    anon = json.loads(api.get("/api/cad/comms", f"view=all&changed={min(cur, anon_cur)}", None)[1])
    assert anon["rows"] == [] and anon["gone"] == []


def test_another_systems_activity_moves_neither_the_cursor_nor_the_rev_of_a_reader_who_may_not_see_it(tmp_path):
    c = make(tmp_path)
    api, who = CadApi(c), Identity(email="someone@example.org")

    def get(path, query, w=None):
        return json.loads(api.get(path, query, w)[1])
    seen = lambda w: (get("/api/cad/comms", "view=all", w)["cursor"], get("/api/cad/comms", "view=all", w)["rev"],   # noqa: E731
                      get("/api/cad/comms", "", w)["rev"], get("/api/cad/comms/sources", "", w)["rev"])
    anon, signed = seen(None), seen(who)
    t = c.clockref.t
    for i in range(5):                                          # another system: rows, a transcript, encrypted key-ups
        c.record_traffic(other(f"pd-busy-{i}", t + 40 + i))
    c.comms.retext("pd-busy-0", "made-up words", "done")
    c.comms.activity("p25:metro:9003", int(t // 60), 4, ["1234"])
    assert seen(None) == anon                                   # nothing to see, nothing to time
    assert seen(who) != signed and seen(who)[0] > signed[0]
    assert get("/api/cad/comms", f"view=all&rev={anon[1]}")["unchanged"] is True
    c.record_traffic(tx("fg-fire", t + 50, rig="E63"))           # a fire row: everyone's
    after = seen(None)
    assert after[0] > anon[0] and after[1] != anon[1] and after[3] != anon[3]
    assert [r["id"] for r in get("/api/cad/comms", f"view=all&changed={anon[0]}")["rows"]] == ["fg-fire"]


def test_an_earlier_builds_triggers_are_taken_off_at_the_start(tmp_path):
    c = make(tmp_path)
    c.store.db.executescript(
        "CREATE TRIGGER radio_changes_t_ins AFTER INSERT ON radio_traffic BEGIN INSERT INTO no_such_table VALUES (1); END;"
        "CREATE TRIGGER radio_changes_a_upd AFTER UPDATE ON alarms BEGIN INSERT INTO no_such_table VALUES (1); END;")
    c.store.db.close()
    again = cad(tmp_path, c.clockref, airband=("Tower",))
    assert not again.store.q("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    assert again.record_traffic(tx("fg-after", c.clockref.t + 60, rig="E63")) == [c.inc["id"]]
    assert not again.comms.changes_broken
    dump = lambda db: "\n".join(db.iterdump())             # noqa: E731
    before = dump(again.store.db)
    again.store.db.close()
    assert dump(cad(tmp_path, c.clockref, airband=("Tower",)).store.db) == before       # a start writes nothing


def test_another_process_holding_the_write_lock_never_holds_a_writer_for_the_change_log(tmp_path):
    """A real second connection takes the database's write lock just as each write has committed (so only the change
    log's own statement meets it): the log gives up at once, the writer is not kept waiting, its data is stored."""
    import time as clock_
    c = make(tmp_path)
    assert c.store.db.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000        # the connection's own: seconds
    holder = sqlite3.connect(c.store.path, isolation_level=None, timeout=0)
    flush, held = c.store.after_commit, []

    def locked_flush():
        holder.execute("BEGIN IMMEDIATE")                       # the lock is the other process's now
        held.append(1)
        try:
            flush()
        finally:
            holder.execute("ROLLBACK")
    c.store.after_commit = locked_flush
    slowest = 0.0
    for name, do, _noted, stored in writers(c):
        c.comms._retry_at = 0.0                                 # every writer meets the lock (not only once a minute)
        t0 = clock_.perf_counter()
        do()                                                    # raises nothing
        slowest = max(slowest, clock_.perf_counter() - t0)
        assert stored(), f"{name} did not store its data"
    assert len(held) >= 20 and slowest < 1.0, f"a writer waited {slowest:.2f} s for the change log"
    assert c.comms.changes_broken and len(c.said) == 1
    assert c.store.db.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000        # put back: the writes' own patience
    c.store.after_commit = flush                                # the lock gone: written again, the gap a reload
    c.comms._retry_at = 0.0
    c.comms.retext("fg-1", "written again", "done")
    assert not c.comms.changes_broken and since(c, 0)[-1] == "fg-1"
    holder.close()
