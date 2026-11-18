"""The console's state push stays cheap when nothing changed (#446): a transaction that writes no row moves no rev,
the alarms' settings are read without every override (thousands of imported place names), and the CAD's broadcaster
re-serializes only the sections that are new objects."""
import json
import sqlite3

from sizeup.cad.service import STATE_CLOCK_S, Cad, Settings
from tests.test_cad import Clock, local, make_cad
from sizeup.cad import settings as settings_mod
from sizeup.web import server as server_mod
from sizeup.web.server import Broadcaster


def make(tmp_path):
    return Cad(Settings(db=str(tmp_path / "cad.sqlite")), log=lambda m: None)


def add_places(cad, n):
    with cad.store.tx() as db:
        for i in range(n):
            db.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES ('places', ?, ?, 0)",
                       (f"sample place {i}", json.dumps({"name": f"Sample Place {i}", "address": f"{i} Example Ave"})))


# -- the rev ------------------------------------------------------------------------------------------------------------

def test_a_transaction_that_writes_nothing_moves_no_rev(tmp_path):
    cad = make(tmp_path)
    r0 = cad.store.rev
    with cad.store.tx() as db:
        db.execute("SELECT COUNT(*) FROM incidents").fetchone()
        db.execute("UPDATE source_reports SET gone_at = NULL WHERE source = 'none' AND ref = 'none'")   # no row
    assert cad.store.rev == r0
    with cad.store.tx() as db:
        db.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES ('x', 'k', '1', 0)")
    assert cad.store.rev == r0 + 1


def test_a_real_change_still_moves_the_rev_and_rebuilds_the_state(tmp_path):
    cad = make(tmp_path)
    st0 = cad.state()
    r0 = cad.store.rev
    cad.create({"type": "ALARM", "location": "1 Example St", "operator": "op"})
    assert cad.store.rev > r0
    st1 = cad.state()
    assert st1["rev"] == cad.store.rev and len(st1["incidents"]) == len(st0["incidents"]) + 1


def test_the_after_commit_hook_runs_even_when_nothing_changed(tmp_path):
    cad = make(tmp_path)
    told = []
    was = cad.store.after_commit
    cad.store.after_commit = lambda: told.append(1)
    try:
        with cad.store.tx():
            pass
    finally:
        cad.store.after_commit = was
    assert told == [1]


def test_a_feed_list_with_nothing_new_moves_no_rev(tmp_path):
    cad = make(tmp_path)
    r0 = cad.store.rev
    for _ in range(3):
        cad.sources.on_seen({"source": "feed1", "ids": ["a", "b"], "complete": True})
    assert cad.store.rev == r0


def test_idle_state_pushes_move_no_rev(tmp_path):
    cad = make(tmp_path)
    cad.state()
    r0 = cad.store.rev
    for _ in range(5):
        cad.state()
    assert cad.store.rev == r0


# -- the overrides: one section, cached until a write ---------------------------------------------------------------------

def test_the_state_push_never_reads_every_override(tmp_path, monkeypatch):
    cad = make(tmp_path)
    add_places(cad, 50)

    read = []                             # (recorded, not raised: Cad.alarms_state swallows an error)
    real = settings_mod.Editor.overrides
    monkeypatch.setattr(settings_mod.Editor, "overrides", lambda self, *a, **kw: read.append(1) or real(self, *a, **kw))
    cad.store.rev += 1                    # a rebuild, and the per-second part
    st = cad.state()
    cad.state()
    assert read == [] and st["alarms"] is not None


def test_a_section_is_read_again_after_any_write(tmp_path):
    cad = make(tmp_path)
    ed = cad.settings
    assert ed.section("alarms") == {}
    with cad.store.tx() as db:            # through a transaction
        db.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES ('alarms', 'drill', 'true', 0)")
    assert ed.section("alarms") == {"drill": True}
    cad.store.db.execute("UPDATE settings_overrides SET value = 'false' WHERE section = 'alarms'")   # autocommit
    assert ed.section("alarms") == {"drill": False}
    other = sqlite3.connect(str(tmp_path / "cad.sqlite"))              # another connection (the replica follower)
    other.execute("UPDATE settings_overrides SET value = '\"x\"' WHERE section = 'alarms'")
    other.commit()
    other.close()
    assert ed.section("alarms") == {"drill": "x"}
    assert cad.alarms.config()["drill"] == "x"


def test_a_section_is_a_copy_and_never_cached_inside_a_transaction(tmp_path):
    cad = make(tmp_path)
    ed = cad.settings
    with cad.store.tx() as db:
        db.execute("INSERT INTO settings_overrides (section, key, value, updated_at) VALUES ('s', 'k', '[1]', 0)")
    got = ed.section("s")
    got["k"].append(2)
    assert ed.section("s") == {"k": [1]}
    try:
        with cad.store.tx() as db:
            db.execute("UPDATE settings_overrides SET value = '[9]' WHERE section = 's'")
            assert ed.section("s") == {"k": [9]}            # read inside: what the transaction sees
            raise RuntimeError("rolled back")
    except RuntimeError:
        pass
    assert ed.section("s") == {"k": [1]}


# -- the broadcaster: a section's JSON kept while it is the same object ---------------------------------------------------

def test_the_cad_broadcaster_serializes_an_unchanged_section_once(monkeypatch):
    big = {"rows": list(range(1000))}
    st = {"time": 0, "incidents": big}
    calls = []
    real = server_mod.json.dumps

    def dumps(v, *a, **kw):
        calls.append(v)
        return real(v, *a, **kw)
    monkeypatch.setattr(server_mod.json, "dumps", dumps)
    b = Broadcaster(lambda: dict(st, time=st["time"]), reuse=True)
    b.publish()
    st["time"] = 1
    seq = b.publish()
    assert sum(1 for v in calls if v is big) == 1                     # kept: the same object
    st["incidents"] = {"rows": [1]}                                   # a new object: made again, and pushed
    calls.clear()
    seq2 = b.publish()
    assert any(v is st["incidents"] for v in calls)
    _, sections, _ = b.snapshot()
    assert json.loads(sections["incidents"][0]) == {"rows": [1]} and sections["incidents"][1] == seq2
    assert sections["time"][1] == seq and seq2 > seq                  # unchanged: still its last change


def test_without_reuse_every_section_is_made_again(monkeypatch):
    big = [1, 2, 3]
    st = {"x": big}
    b = Broadcaster(lambda: st)
    b.publish()
    big.append(4)                         # changed in place: the App's state may do this
    b.publish()
    _, sections, _ = b.snapshot()
    assert json.loads(sections["x"][0]) == [1, 2, 3, 4]


def test_cad_state_sections_are_the_same_objects_while_nothing_changed(tmp_path):
    """What reuse relies on: Cad.state hands back its cached sections as they are until the rev moves."""
    cad = make(tmp_path)
    cad.create({"type": "ALARM", "location": "1 Example St", "operator": "op"})
    a, b = cad.state(), cad.state()
    assert a["incidents"] is b["incidents"] and a["lists"] is b["lists"] and a["run_cards"] is b["run_cards"]
    cad.create({"type": "ALARM", "location": "2 Example St", "operator": "op"})
    assert cad.state()["incidents"] is not a["incidents"]


# -- what ages with the clock, with no write to move the rev ---------------------------------------------------------------

def test_a_closed_call_leaves_the_queue_after_its_hours_with_no_write(tmp_path):
    clock = Clock(local(2026, 10, 7, 14, 0))
    cad = make_cad(tmp_path, clock=clock)
    iid = cad.create({"type": "MVA", "address": "Example Rd"})["id"]
    cad.close(iid, {"disposition": "COMPLETE", "operator": "JD"})
    assert iid in [i["id"] for i in cad.state()["incidents"]]
    clock.t += 3 * 86400
    with cad.store.tx() as db:            # what used to move the rev all day: a write of nothing
        db.execute("SELECT 1").fetchone()
    assert iid not in [i["id"] for i in cad.state()["incidents"]]


def test_the_state_is_rebuilt_each_minute_and_kept_within_one(tmp_path):
    clock = Clock(local(2026, 10, 7, 14, 0))
    cad = make_cad(tmp_path, clock=clock)
    a = cad.state()
    clock.t += 1
    assert cad.state()["incidents"] is a["incidents"]               # the same minute: kept
    clock.t += STATE_CLOCK_S
    assert cad.state()["incidents"] is not a["incidents"]           # the next: built again
    built = []
    real = cad.store.recent_clears
    cad.store.recent_clears = lambda since: built.append(since) or real(since)
    cad.clears(clock.t)                    # (the state's build read them this minute)
    cad.clears(clock.t + 1)
    assert built == []
    cad.clears(clock.t + STATE_CLOCK_S)
    assert len(built) == 1


def test_a_preplan_write_moves_the_rev_after_its_gen(tmp_path):
    """A state built between the write's commit and the gen's move holds the old gen: a rev after the gen builds it
    again."""
    cad = make(tmp_path)
    seen = []
    real = cad.preplan._bump

    def bump():                           # a broadcaster's build right in the gap
        seen.append(cad.state()["preplan"]["gen"])
        real()
    cad.preplan._bump = bump
    g0 = cad.preplan.gen
    cad.preplan.create({"kind": "knox_box", "lat": 41.0, "lon": -73.66, "label": "Front door"})
    assert seen == [g0]
    assert cad.state()["preplan"]["gen"] == cad.preplan.gen != g0
    r0 = cad.store.rev
    cad.preplan_docs._bump()
    assert cad.store.rev == r0 + 1 and cad.state()["preplan_docs"]["gen"] == cad.preplan_docs.gen
