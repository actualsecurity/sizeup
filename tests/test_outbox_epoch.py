"""The station's outbox file across a stop that was not in order, a row it cannot read and a file SQLite cannot open
(sizeup/cloud/outbox.py: the openers' markers and the epoch, batch()'s unreadable rows, open_outbox), and the hint on
Health (sizeup/cloud/health.py). contracts/README.md O1, O3, O4. Invented events only."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from sizeup.cloud import outbox as outbox_mod
from sizeup.cloud.outbox import Outbox, open_outbox
from tests.test_health import T0, Rig, station_status
from tests.test_outbox_poison import FakeHub, drain, link  # noqa: F401 - the fixture

T = 1_790_000_000.0


def markers(path) -> dict:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta WHERE key LIKE 'open:%'")}
    finally:
        db.close()


def cut(path, into, keep_wal: int) -> None:
    """The power goes: the main file as it is, the log only as far as the disk had it."""
    into.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(path, into)
    wal = path.with_name(path.name + "-wal").read_bytes()
    into.with_name(into.name + "-wal").write_bytes(wal[:keep_wal])


# -- O1: a seq the hub can have is never handed out again under the same epoch ---------------------------------------

def test_a_file_set_back_by_a_power_cut_gets_a_new_epoch_and_a_clean_stop_keeps_it(tmp_path):
    path = tmp_path / "st" / "outbox.sqlite"
    ob = Outbox(path)
    epoch = ob.epoch
    assert ob.add("page", {"id": "a1b2c3d4e5"}, at=T) == 1 and [r["seq"] for r in ob.batch()] == [1]
    ob.ack(1)
    synced = path.with_name("outbox.sqlite-wal").stat().st_size
    assert ob.add("page", {"id": "f6a7b8c9d0"}, at=T + 1) == 2 and [r["seq"] for r in ob.batch()] == [2]
    ob.ack(2)                                             # the hub's cursor: (epoch, 2)
    cut(path, tmp_path / "cut" / "outbox.sqlite", synced)
    assert len(markers(path)) == 1                        # (armed while open)
    ob.close()
    assert markers(path) == {}                            # stopped in order: no marker left
    again = Outbox(path)                                  # a clean stop: the same epoch, the next seq
    try:
        assert again.epoch == epoch and again.unclean == "" and again.notes == []
        assert again.add("heard", {"radio_id": "990001"}, at=T + 2) == 3
    finally:
        again.close()
    logs: list[str] = []
    back = Outbox(tmp_path / "cut" / "outbox.sqlite", log=logs.append)
    try:
        assert back.pending()[0] == 0                     # (set back to after the first ack: seq 2 would come again)
        assert back.epoch != epoch and back.unclean       # ... under a new epoch: the hub counts it from the start
        assert back.add("page", {"id": "0a1b2c3d4e"}, at=T + 60) == 2
        assert [n["text"] for n in back.notes] == ["new epoch: the last run did not stop in order"]
        assert len(logs) == 1 and "did not stop in order" in logs[0]
        assert list(markers(tmp_path / "cut" / "outbox.sqlite")) == ["open:" + back._token]   # the stale one gone
    finally:
        back.close()
    third = Outbox(tmp_path / "cut" / "outbox.sqlite")    # ... and that epoch stays, once stopped in order
    try:
        assert third.epoch == back.epoch and not third.unclean
    finally:
        third.close()


def test_a_marker_of_another_boot_or_of_a_process_that_is_gone_is_a_stop_not_in_order(tmp_path, monkeypatch):
    path = tmp_path / "st" / "outbox.sqlite"
    Outbox(path).close()
    gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    cases = {"another boot": {"boot": "not-this-boot", "pid": os.getpid()},
             "a process that is gone": {"boot": outbox_mod.boot_id(), "pid": int(gone.stdout)},
             "a marker that does not parse": "{"}
    for why, value in cases.items():
        ob = Outbox(path)
        epoch = ob.epoch
        ob.close()
        db = sqlite3.connect(path)
        db.execute("INSERT INTO meta (key, value) VALUES ('open:0123456789abcdef', ?)",
                   (value if isinstance(value, str) else json.dumps(value),))
        db.commit()
        db.close()
        ob = Outbox(path)
        try:
            assert ob.epoch != epoch and ob.unclean, why
            assert list(markers(path)) == ["open:" + ob._token], why
        finally:
            ob.close()


def test_an_opener_that_is_still_there_keeps_the_epoch(tmp_path):
    """Two openers in one process (as built: nothing gives the file one owner), and the quarantine command beside a
    running station (another process in this boot): the same epoch, and each one's marker is its own."""
    path = tmp_path / "st" / "outbox.sqlite"
    one = Outbox(path)
    two = Outbox(path)
    try:
        assert one.epoch == two.epoch and not two.unclean and len(markers(path)) == 2
        two.close()
        assert list(markers(path)) == ["open:" + one._token]
        cli = subprocess.run([sys.executable, "-m", "sizeup.cloud.outbox", "--path", str(path), "list"],
                             capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)))
        assert cli.returncode == 0 and "nothing refused by the hub" in cli.stdout, cli.stderr
        assert list(markers(path)) == ["open:" + one._token]
        three = Outbox(path)
        try:
            assert three.epoch == one.epoch and not three.unclean
        finally:
            three.close()
    finally:
        one.close()


def test_a_parent_process_marker_is_live_while_it_runs(tmp_path):
    """Another process in this boot that still runs (here: the one that started this test): its marker is no stop."""
    path = tmp_path / "st" / "outbox.sqlite"
    ob = Outbox(path)
    epoch = ob.epoch
    ob.close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO meta (key, value) VALUES ('open:00000000000000aa', ?)",
               (json.dumps({"boot": outbox_mod.boot_id(), "pid": os.getppid()}),))
    db.commit()
    db.close()
    ob = Outbox(path)
    try:
        assert ob.epoch == epoch and not ob.unclean and len(markers(path)) == 2
    finally:
        ob.close()


def test_released_at_the_stop_nothing_goes_out_until_it_resumes_armed_again(tmp_path):
    """StationMode.stop lets the outbox go (release): the marker is out while the file stays open, and batch() hands
    out nothing more (a forwarder still between its turns writes no marker again). resume() (StationMode.start): the
    next batch() arms it first, so a seq never goes out unmarked."""
    path = tmp_path / "st" / "outbox.sqlite"
    ob = Outbox(path)
    try:
        ob.add("heard", {"radio_id": "990001"}, at=T)
        ob.release()
        assert markers(path) == {}
        other = Outbox(path)                              # (stopped in order: the same epoch)
        assert other.epoch == ob.epoch and not other.unclean
        other.close()
        ob.add("heard", {"radio_id": "990002"}, at=T + 1)  # an add after the stop: no fsync, nothing goes out
        assert ob.batch() == [] and markers(path) == {} and ob.pending()[0] == 2
        ob.resume()
        assert [r["seq"] for r in ob.batch()] == [1, 2]
        assert list(markers(path)) == ["open:" + ob._token]
        ob.release()
        ob.release()                                      # (twice: nothing more)
    finally:
        ob.close()
    assert markers(path) == {}


def test_a_marker_it_cannot_write_at_the_start_never_stops_the_station(tmp_path, monkeypatch):
    """A full disk at the start: the marker's commit fails. The outbox opens on the epoch it has (said in the log), and
    batch() arms first: it raises until it can (the forwarder starts again), and a stale marker found then still gives
    a new epoch before any seq goes out."""
    path = tmp_path / "st" / "outbox.sqlite"
    ob = Outbox(path)
    ob.add("heard", {"radio_id": "990001"}, at=T)
    epoch = ob.epoch
    ob.db.execute("INSERT INTO meta (key, value) VALUES ('open:00000000000000bb', ?)",
                  (json.dumps({"boot": "not-this-boot", "pid": 1}),))
    ob.close()
    real, full = Outbox._durable, [True]

    def durable(self, fn):
        if full[0]:
            raise sqlite3.OperationalError("database or disk is full")
        return real(self, fn)
    monkeypatch.setattr(Outbox, "_durable", durable)
    logs: list[str] = []
    ob = Outbox(path, log=logs.append)
    try:
        assert ob.epoch == epoch and not ob._armed and "marker not written" in logs[0]
        with pytest.raises(sqlite3.OperationalError):
            ob.batch()
        full[0] = False
        assert [r["seq"] for r in ob.batch()] == [1]
        assert ob.epoch != epoch and ob.unclean and list(markers(path)) == ["open:" + ob._token]
    finally:
        ob.close()


def test_a_rollback_after_a_failed_commit_keeps_the_real_error(tmp_path):
    ob = Outbox(tmp_path / "st" / "outbox.sqlite")
    try:
        def fails():
            ob.db.execute("ROLLBACK")                     # (as SQLite does itself when a COMMIT fails on a full disk)
            raise sqlite3.OperationalError("database or disk is full")
        with pytest.raises(sqlite3.OperationalError, match="disk is full"):
            ob._durable(fails)
        assert ob.db.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        ob.close()


def test_the_quarantine_command_reads_the_epoch_and_touches_no_marker(tmp_path):
    path = tmp_path / "st" / "outbox.sqlite"
    ob = Outbox(path)
    epoch = ob.epoch
    ob.close()
    db = sqlite3.connect(path)
    db.execute("INSERT INTO meta (key, value) VALUES ('open:00000000000000cc', ?)",
               (json.dumps({"boot": "not-this-boot", "pid": 1}),))
    db.commit()
    db.close()
    assert outbox_mod.main(["--path", str(path), "list"]) == 0
    assert list(markers(path)) == ["open:00000000000000cc"]      # (not its to judge: no seq goes out from it)
    cli = Outbox(path, arm=False)
    try:
        assert cli.epoch == epoch and not cli._armed
    finally:
        cli.close()
    assert outbox_mod._marker_live("x", json.dumps({"boot": outbox_mod.boot_id(), "pid": 0})) is False
    assert outbox_mod._marker_live("x", json.dumps({"boot": outbox_mod.boot_id(), "pid": -1})) is False


def test_the_station_lets_its_outbox_go_when_it_stops(tmp_path):
    from sizeup.app import make_app
    from tests.test_cloud import free_port, station_cfg
    app = make_app(station_cfg(tmp_path, f"http://127.0.0.1:{free_port()}"))
    ob = app.station.outbox()
    try:
        assert ob is app.link.outbox and len(markers(ob.path)) == 1
        app.stop()                                        # (the whole stop: StationMode.stop lets it go)
        assert markers(ob.path) == {} and ob.batch() == []
    finally:
        app.link.stop()
        ob.close()


def test_an_older_builds_file_and_a_memory_outbox_keep_their_epoch(tmp_path):
    path = tmp_path / "st" / "outbox.sqlite"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                     "INSERT INTO meta VALUES ('epoch', '00112233445566ff');")
    db.close()
    ob = Outbox(path)
    try:
        assert ob.epoch == "00112233445566ff" and not ob.unclean
    finally:
        ob.close()
    mem = Outbox(":memory:")
    try:
        assert len(mem.epoch) == 16 and mem.add("heard", {}, at=T) == 1 and mem.batch()[0]["seq"] == 1
        mem.release()
    finally:
        mem.close()


# -- O3: a row the station cannot read is set aside, the rows behind it go -------------------------------------------

def test_rows_it_cannot_read_are_set_aside_and_the_rest_go_in_order(link, monkeypatch):  # noqa: F811
    ob = link.outbox
    logs: list[str] = []
    ob.log = logs.append
    ob.add("traffic", {"id": "FG1-1"}, at=T)
    ob.db.execute("INSERT INTO outbox (at, kind, data) VALUES (?, 'heard', 'not json')", (T + 1,))
    ob.db.execute("INSERT INTO outbox (at, kind, data, audio) VALUES (?, 'traffic', '{\"id\": \"FG1-3\"}', 'RIFF')",
                  (T + 2,))
    ob.db.execute("INSERT INTO outbox (at, kind, data) VALUES ('soon', 'heard', '{}')")
    ob.db.execute("INSERT INTO outbox (at, kind, data) VALUES (?, 'source_event', 'not json either')", (T + 3,))
    ob.add("page", {"id": "South2-6"}, at=T + 4)
    hub = FakeHub()
    drain(link, hub)
    assert hub.got == ["FG1-1", "South2-6"] and ob.pending()[0] == 0 and hub.acked == 6
    q = ob.summary()
    assert q["n"] == 4 and q["critical"] == 0
    assert {r["reason"] for r in q["rows"]} == {"unreadable"} and {r["state"] for r in q["rows"]} == {"held"}
    kept = ob.db.execute("SELECT seq, data, typeof(audio) FROM quarantine ORDER BY seq").fetchall()
    assert kept[:2] == [(2, '{"unreadable":"not json"}', "null"),                    # kept, for a person
                        (3, '{"unreadable":"{\\"id\\": \\"FG1-3\\"}"}', "text")]
    assert ob.unreadable == 4 and len(logs) == 2                  # once per kind of error (JSONDecodeError, TypeError)
    assert [n["text"] for n in ob.notes] == ["unreadable heard #2 set aside (JSONDecodeError)",
                                             "unreadable traffic #3 set aside (TypeError)"]
    # a later source_event the hub refuses is quarantined beside the unreadable one (its data is never parsed)
    ob.add("source_event", {"source": "east-cad", "event": {"event_id": "E-1"}}, at=T + 5)
    row = ob.batch(meta=True)[0]
    assert ob.quarantine(row, "apply_failed", "x")["critical"] is True
    assert ob.requeue_due(now=T + 10 ** 9)[0]["superseded"] is False


def test_a_requeued_copy_it_cannot_read_turns_its_own_row_and_is_never_sent_again(tmp_path):
    ob = Outbox(tmp_path / "st" / "outbox.sqlite")
    try:
        ob.add("page", {"id": "South2-1"}, at=T)
        q = ob.quarantine(ob.batch(meta=True)[0], "apply_failed", "x", now=T)
        assert q["critical"] is True
        ob.db.execute("UPDATE quarantine SET data = 'not json' WHERE id = ?", (q["id"],))
        for k in range(4):
            ob.requeue_due(now=T + 10 ** 6 * (k + 1))
            assert ob.batch() == []
        rows = ob.db.execute("SELECT reason, critical, next_at, data FROM quarantine").fetchall()
        assert rows == [("unreadable", 0, None, '{"unreadable":"not json"}')] and ob.pending()[0] == 0
    finally:
        ob.close()


def test_a_window_of_rows_it_cannot_read_never_stops_the_batch(tmp_path):
    ob = Outbox(tmp_path / "st" / "outbox.sqlite")
    try:
        for _ in range(5):
            ob.db.execute("INSERT INTO outbox (at, kind, data) VALUES (?, 'heard', '{')", (T,))
        ob.add("heard", {"radio_id": "990001"}, at=T + 1)
        assert [r["seq"] for r in ob.batch(max_events=2)] == [6] and ob.unreadable == 5
        assert ob.pending()[0] == 1
    finally:
        ob.close()


def test_a_speech_to_text_input_it_cannot_read_is_dropped_and_the_lane_goes_on(tmp_path):
    ob = Outbox(tmp_path / "st" / "outbox.sqlite")
    try:
        ob.db.execute("INSERT INTO stt_inputs (at, data, flac, meta) VALUES (?, 'not json', x'00', '{}')", (T,))
        ob.db.execute("INSERT INTO stt_inputs (at, data, flac, meta) VALUES (?, '{}', 'text', '{}')", (T,))
        ob.add_stt({"id": "a1b2c3d4e5"}, b"\xc3" * 12, {"v": 1}, at=T)
        assert [r["n"] for r in ob.stt_batch(now=T)] == [3] and ob.stt_pending()[0] == 1
        assert [n["text"] for n in ob.notes] == ["unreadable STT input #1 dropped (JSONDecodeError)"]
    finally:
        ob.close()


# -- O4: a file SQLite cannot open is moved aside, kept, and a new outbox starts --------------------------------------

def test_a_file_sqlite_cannot_open_is_moved_aside_and_a_new_outbox_starts(tmp_path):
    path = tmp_path / "st" / "outbox.sqlite"
    path.parent.mkdir()
    junk = b"not an SQLite file " * 300
    path.write_bytes(junk)
    path.with_name("outbox.sqlite-wal").write_bytes(b"log")
    logs: list[str] = []
    ob = open_outbox(path, log=logs.append)
    try:
        assert ob.add("heard", {}, at=T) == 1 and len(ob.epoch) == 16
        aside = sorted(p.name for p in path.parent.iterdir() if ".unreadable-" in p.name)
        assert aside[1:] == [aside[0] + "-shm", aside[0] + "-wal"]                # the log goes with it ...
        assert (path.parent / aside[0]).read_bytes() == junk                      # ... kept as it was
        assert (path.parent / aside[2]).read_bytes() == b"log"
        assert len(logs) == 1 and "could not be opened" in logs[0] and aside[0] in logs[0]
        assert ob.notes[0]["text"] == f"file unreadable: moved aside as {aside[0]}, new outbox"
    finally:
        ob.close()


def test_what_moving_the_file_would_not_mend_still_raises(tmp_path):
    """A folder in the file's place, a file it may not open: SQLite's own error, nothing moved."""
    path = tmp_path / "st" / "outbox.sqlite"
    path.mkdir(parents=True)
    with pytest.raises(sqlite3.OperationalError):
        open_outbox(path)
    assert [p.name for p in path.parent.iterdir()] == ["outbox.sqlite"]
    assert outbox_mod._cannot_open(sqlite3.OperationalError("database is locked")) is False


def test_the_station_starts_over_a_file_sqlite_cannot_open(tmp_path):
    from sizeup.app import make_app
    from tests.test_cloud import free_port, station_cfg
    cfg = station_cfg(tmp_path, f"http://127.0.0.1:{free_port()}")
    path = tmp_path / "st" / "outbox.sqlite"
    assert cfg.cloud_outbox == str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 4096 + b"not a database")
    app = make_app(cfg)
    try:
        ob = app.link.outbox
        assert ob.add("heard", {}, at=T) == 1
        notes = app.link.status(talkgroups=False)["outbox"]["notes"]
        assert len(notes) == 1 and notes[0]["text"].startswith("file unreadable: moved aside as outbox.sqlite.unreadable-")
    finally:
        app.link.stop()
        app.link.outbox.close()


def test_the_outboxs_notes_are_a_hint_on_health_for_a_day():
    r = Rig()
    st = station_status(outbox={"pending": 0, "oldest": None, "dropped_audio": 0,
                                "notes": [{"at": T0 - 60, "text": "new epoch: the last run did not stop in order"},
                                          {"at": T0 - 2 * 86400, "text": "old"}, {"at": "x", "text": 5}]})
    r.at(0, status=st)
    hints = [h for h in r.h.model()["hints"] if h["channel"] == "Station outbox"]
    assert len(hints) == 1 and hints[0]["text"].endswith(": new epoch: the last run did not stop in order")
    assert r.keys() == []                                             # a hint, never an alert
