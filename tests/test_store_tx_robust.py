"""Store.tx() when BEGIN or COMMIT fails, the lock on Store reads, and the CAD's shutdown (its workers joined before the
store closes: the test-teardown segfault). Synthetic only."""

from __future__ import annotations

import faulthandler
import sqlite3
import threading

import pytest

from sizeup.cad.store import Store
from tests.test_cad import make_cad

faulthandler.enable()


def lock_free(store: Store, timeout: float = 2.0) -> bool:
    """Can another thread take the store's lock? (An RLock: this thread always could.)"""
    got: list[bool] = []

    def take():
        ok = store.lock.acquire(timeout=timeout)
        got.append(ok)
        if ok:
            store.lock.release()
    t = threading.Thread(target=take)
    t.start()
    t.join()
    return got == [True]


def hooks(store: Store) -> list[str]:
    fired: list[str] = []
    store.after_commit = lambda: fired.append("commit")
    store.after_rollback = lambda: fired.append("rollback")
    return fired


# -- BEGIN fails -----------------------------------------------------------------------------------------------------


def test_begin_busy_releases_the_lock(tmp_path):
    s = Store(tmp_path / "cad.sqlite")
    s.db.execute("PRAGMA busy_timeout = 50")
    other = sqlite3.connect(tmp_path / "cad.sqlite", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")                       # another writer holds the write lock
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with s.tx():
            pytest.fail("never entered")
    assert lock_free(s) and not s.db.in_transaction
    other.execute("ROLLBACK")
    other.close()
    rev = s.rev
    with s.tx() as db:                                     # the next one works
        db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    assert s.rev == rev + 1 and s.meta_get("k") == "v"
    s.shutdown()


def test_begin_inside_an_open_transaction_releases_the_lock():
    s = Store()
    s.db.execute("BEGIN")                                  # left open by someone else
    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        with s.tx():
            pytest.fail("never entered")
    assert lock_free(s)
    s.db.execute("ROLLBACK")
    with s.tx() as db:
        db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    assert s.meta_get("k") == "v"


# -- COMMIT fails ----------------------------------------------------------------------------------------------------


def test_commit_failure_rolls_back_and_releases_the_lock():
    s = Store()
    with s.tx() as db:                                     # a deferred foreign key: checked only at COMMIT
        db.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE c (pid INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED)")
    fired, rev = hooks(s), s.rev
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with s.tx() as db:
            db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
            db.execute("INSERT INTO c (pid) VALUES (999)")
    assert not s.db.in_transaction                         # rolled back, not left dangling
    assert fired == ["rollback"] and s.rev == rev
    assert lock_free(s)
    s.meta_put("later", "1")                               # an autocommit write after it is its own, and stays
    assert s.meta_get("k") is None and s.meta_get("later") == "1"
    with s.tx() as db:
        db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    assert fired == ["rollback", "commit"] and s.rev == rev + 1


def test_hooks_unchanged_on_success_and_on_the_callers_error():
    s = Store()
    fired = hooks(s)
    with s.tx() as db:
        db.execute("INSERT INTO meta (key, value) VALUES ('a', '1')")
    with pytest.raises(ValueError):
        with s.tx() as db:
            db.execute("INSERT INTO meta (key, value) VALUES ('b', '1')")
            raise ValueError
    assert fired == ["commit", "rollback"] and s.meta_get("b") is None and lock_free(s)


# -- reads under the lock ----------------------------------------------------------------------------------------------


def test_dept_signed_on_waits_for_the_lock():
    s = Store()
    done = threading.Event()

    def read():
        s.dept_signed_on(1, "DPTA")
        done.set()
    with s.lock:
        t = threading.Thread(target=read)
        t.start()
        assert not done.wait(0.3)                          # held elsewhere: it waits
    assert done.wait(2)
    t.join()


# -- shutdown ------------------------------------------------------------------------------------------------------


def test_store_shutdown_is_idempotent(tmp_path):
    s = Store(tmp_path / "cad.sqlite")
    s.shutdown()
    s.shutdown()
    with pytest.raises(sqlite3.ProgrammingError):
        s.q("SELECT 1")


def queue_units_work(c) -> int:
    """An incident with units on it: the radio log's units worker has work queued (not inline)."""
    iid = c.store.create_incident(type="ALARM", subtype="COMM", address="100 Example Ave")
    uids = [u["id"] for u in c.store.units()[:3]]
    c.store.assign(iid, uids, operator="Test")             # on_assigned: radiolog.units_soon, off this thread
    for _ in range(5):
        c.radiolog.units_soon(iid)
    c.radiolog.backfill_soon(iid)
    return iid


def test_cad_shutdown_joins_the_workers_and_is_idempotent(tmp_path):
    c = make_cad(tmp_path)
    assert not c.radiolog.inline
    queue_units_work(c)
    threads = list(c.radiolog._threads)
    assert {t.name for t in threads} >= {"radiolog-units", "radiolog-backfill"}
    c.shutdown()
    assert not any(t.is_alive() for t in threads)
    assert c.voice.stopped and c.voice.join(0)
    with pytest.raises(sqlite3.ProgrammingError):
        c.store.q("SELECT 1")
    c.shutdown()                                           # again: nothing
    c.radiolog.units_soon(1)                               # stopped: no new worker
    assert not any(t.is_alive() for t in c.radiolog._threads)


def test_shutdown_with_work_queued_never_crashes(tmp_path):
    """The teardown segfault: the connection closed while the units worker still used it. 100 rounds."""
    for i in range(100):
        c = make_cad(tmp_path / str(i))
        queue_units_work(c)
        c.shutdown()
        assert not any(t.is_alive() for t in c.radiolog._threads)


# -- review follow-ups -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["after_commit", "after_rollback"])
def test_a_hook_raising_a_base_exception_never_keeps_the_lock(which):
    s = Store()

    def boom():
        raise KeyboardInterrupt
    setattr(s, which, boom)
    with pytest.raises(KeyboardInterrupt):
        with s.tx() as db:
            db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
            if which == "after_rollback":
                raise ValueError
    assert lock_free(s) and not s.db.in_transaction


def test_the_callers_error_is_raised_when_its_rollback_fails_too():
    """The transaction already ended under the caller (SQLite rolls back by itself on some FULL / IOERR): the ROLLBACK
    is skipped, and the caller's own error is the one raised, not "no transaction is active"."""
    s = Store()
    fired = hooks(s)
    with pytest.raises(ValueError, match="the caller's"):
        with s.tx() as db:
            db.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
            db.execute("ROLLBACK")
            raise ValueError("the caller's")
    assert fired == ["rollback"] and lock_free(s) and not s.db.in_transaction and s.meta_get("k") is None


def test_a_plain_stop_still_backfills_a_first_report_a_shutdown_does_not(tmp_path):
    c = make_cad(tmp_path)
    iid = c.store.create_incident(type="ALARM", subtype="COMM", address="100 Example Ave")
    c.radiolog.stop()                                      # the App's stop: a first report's backfill still runs
    c.radiolog.backfill_soon(iid)
    assert any(t.name == "radiolog-backfill" for t in c.radiolog._threads)
    c.radiolog.units_soon(iid)                             # ... the units worker does not (picked up at the next start)
    assert not any(t.name == "radiolog-units" for t in c.radiolog._threads)
    c.shutdown()
    n = len(c.radiolog._threads)
    c.radiolog.backfill_soon(iid)                          # shutting down: nothing new
    assert len(c.radiolog._threads) == n and not any(t.is_alive() for t in c.radiolog._threads)


def test_shutdown_closes_the_store_when_a_stop_fails(tmp_path):
    logged: list[str] = []
    c = make_cad(tmp_path)
    c.log = logged.append

    def broken():
        raise RuntimeError("stuck")
    c.addrs.stop = broken
    c.shutdown()
    assert any("address history: RuntimeError" in m for m in logged)
    assert c.voice.stopped                                 # the steps after it still ran
    with pytest.raises(sqlite3.ProgrammingError):
        c.store.q("SELECT 1")


def test_address_history_stop_from_its_own_thread(tmp_path):
    c = make_cad(tmp_path)
    c.addrs._thread = threading.current_thread()          # as if its own worker stopped it: no join on itself
    c.addrs.stop()
    c.shutdown()
