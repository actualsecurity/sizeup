"""What a hub start writes to its databases (docs/cloud.md "Replicate before migrate", the 2026-11 outage).

A takeover's `--init-state` work (sizeup/app.py init_state: the schema migrations of every CAD part) runs on the
promoted copy of the replica. Since the outage it runs only once Litestream replicates the file (sizeup/handoff.py
_start_writer), but a start that rewrites a big table still costs the takeover its time (INIT_S) and S3 a whole
copy of the table (REPLICATING_S), at every deploy. So:

- the guard: a start on a database already at this build's schema writes next to nothing, however big the radio
  log is. A migration that rewrites rows, rebuilds an index or recreates a table at every start (the shape of #419,
  #441, #482) fails here; a one-time migration must leave nothing for the next start to do;
- the hazard itself, in plain SQLite: a migration with WAL autocheckpoints moves its pages into the main file and
  leaves no WAL behind, so nothing Litestream starts on afterwards could read them as changes; the supervisor's
  fingerprint of the promoted file (sizeup/handoff.py fingerprint / _check_untouched) is what notices.

Counting: a reader holding a read transaction (as Litestream does) keeps every frame the start writes in the WAL.
Every transmission here is invented."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sizeup.app import init_state
from sizeup.handoff import fingerprint, sidecars
from tests.test_radios import Clock, cad, tx
from tests.test_startup import hub_cfg

STEADY_FRAMES = 16          # a start on an up-to-date database: a few pages at most (today: none), never a table
ROWS = 3000                 # the radio log behind it: a rewrite of it is hundreds of pages


def wal_frames_written(paths: list[Path], fn) -> dict[str, int]:
    """The WAL frames fn() writes to each file (pages, counted once per commit that wrote them). A reader with an
    open read transaction (Litestream's) keeps SQLite from checkpointing or restarting the WAL meanwhile."""
    readers = []
    for p in paths:
        r = sqlite3.connect(p, isolation_level=None)
        r.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        r.execute("BEGIN")
        r.execute("SELECT count(*) FROM sqlite_master").fetchone()
        readers.append(r)
    try:
        fn()
        out = {}
        for p in paths:
            wal = Path(f"{p}-wal")
            size = wal.stat().st_size if wal.exists() else 0
            page = sqlite3.connect(f"file:{p}?mode=ro", uri=True).execute("PRAGMA page_size").fetchone()[0]
            out[p.stem] = max(0, size - 32) // (page + 24)
        return out
    finally:
        for r in readers:
            r.close()


def hub_with_a_radio_log(tmp_path: Path):
    cfg = hub_cfg(tmp_path)
    init_state(cfg)
    clock = Clock()
    c = cad(Path(cfg.cad_db).parent, clock)
    for i in range(ROWS):
        c.record_traffic(tx(f"fg-{i}", clock.t - 10 * i, rig="E63" if i % 3 == 0 else "", radio_id="4242098" if i % 2 else "",
                            text=f"made-up transmission number {i}: engine on location, water supply established, "
                                 "primary search under way, all units hold your positions"))
    c.store.db.close()
    state = Path(cfg.cad_db).parent
    return cfg, [state / f"{n}.sqlite" for n in ("cad", "hub", "audio")]


def test_a_start_on_an_up_to_date_database_rewrites_nothing(tmp_path):
    cfg, paths = hub_with_a_radio_log(tmp_path)
    pages = sqlite3.connect(paths[0]).execute("PRAGMA page_count").fetchone()[0]
    assert pages > 4 * STEADY_FRAMES                                       # a rewrite would show
    written = wal_frames_written(paths, lambda: init_state(cfg))
    big = {db: n for db, n in written.items() if n > STEADY_FRAMES}
    assert not big, (f"a hub start on an up-to-date database wrote {big} WAL frames (limit {STEADY_FRAMES}): a "
                     "migration that rewrites rows / rebuilds an index / recreates a table at every start. Make it "
                     "one-time (nothing left for the next start), and see docs/cloud.md \"Replicate before migrate\"")


def test_the_guard_sees_a_full_table_rewrite(tmp_path):
    cfg, paths = hub_with_a_radio_log(tmp_path)

    def rewrite():                                                          # the shape of #419's backfill, every start
        init_state(cfg)
        db = sqlite3.connect(paths[0], isolation_level=None)
        db.execute("UPDATE radio_traffic SET tier = 'text' WHERE tier = ''")
        db.close()
    assert wal_frames_written(paths, rewrite)["cad"] > 2 * STEADY_FRAMES


def test_a_migration_with_autocheckpoints_leaves_its_pages_in_the_main_file_and_no_wal(tmp_path):
    """The hazard: promoted, then migrated before Litestream ran. Afterwards there is no WAL for Litestream to read
    the migration from: it is in the main file only. The promoted file's fingerprint changed, which is what the
    supervisor checks before `litestream replicate` (else "written before Litestream replicates it")."""
    _, paths = hub_with_a_radio_log(tmp_path)
    cad_db = paths[0]
    promoted = fingerprint(cad_db)
    db = sqlite3.connect(cad_db, isolation_level=None)
    db.execute("PRAGMA wal_autocheckpoint = 100")                           # SQLite's checkpoints, as a big migration has
    for k in range(4):
        db.execute(f"ALTER TABLE radio_traffic ADD COLUMN made_up_{k} TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE INDEX made_up_index ON radio_traffic(made_up_0, start)")
    for k in range(3):
        db.execute(f"UPDATE radio_traffic SET made_up_{k} = channel || '-' || id")
    db.close()                                                              # the last connection: checkpointed, WAL gone
    assert sidecars(cad_db) == []                                           # nothing left in any WAL
    assert fingerprint(cad_db) != promoted                                  # ... the main file has it all
    assert sqlite3.connect(cad_db).execute("PRAGMA quick_check").fetchone()[0] == "ok"
