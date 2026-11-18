-- The station's outbox file (state/outbox.sqlite) as sizeup/cloud/outbox.py makes a new one. RECORDED from a real
-- Outbox by `python -m tests.contracts record` (tests/test_contracts_outbox.py), never edited by hand. It is a script:
-- run on an empty SQLite file it makes a file the station opens as its own. columns.json beside it says the same
-- column by column: a file made any other way (by an older build and then altered, by another program) is held to that.

-- In the file's header: every connection finds these.
PRAGMA encoding = 'UTF-8';
PRAGMA auto_vacuum = 0;             -- none: the file does not shrink when rows go (its pages are used again)
PRAGMA journal_mode = wal;          -- a write-ahead log: outbox.sqlite-wal and -shm beside the file while open
PRAGMA application_id = 0;          -- never set
PRAGMA user_version = 0;            -- never set, never read: the file carries no version

-- Per connection: the station sets it every time it opens the file; another connection has its SQLite's default.
PRAGMA synchronous = 1;             -- NORMAL: in WAL mode a commit is not synced to the disk (README, O1)

-- sqlite_master, in the order the objects were made.
CREATE TABLE outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    audio BLOB,
    audio_dropped INTEGER NOT NULL DEFAULT 0
, context INTEGER NOT NULL DEFAULT 0, qid INTEGER);
-- SQLite's own, made with the first AUTOINCREMENT table. The next seq and the next n are kept in it: never MAX + 1.
--   CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- SQLite's own index for meta's primary key: sqlite_autoindex_meta_1
CREATE TABLE stt_inputs (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    data TEXT NOT NULL,
    flac BLOB NOT NULL,
    meta TEXT NOT NULL,
    prio INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    audio BLOB,
    audio_dropped INTEGER NOT NULL DEFAULT 0,
    context INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    error TEXT NOT NULL,
    first_at REAL NOT NULL,
    last_at REAL NOT NULL,
    tries INTEGER NOT NULL DEFAULT 1,
    critical INTEGER NOT NULL DEFAULT 0,
    next_at REAL,
    state TEXT NOT NULL DEFAULT 'held'
);
CREATE TABLE source_latest (key TEXT PRIMARY KEY, seq INTEGER NOT NULL, at REAL NOT NULL);
-- SQLite's own index for source_latest's primary key: sqlite_autoindex_source_latest_1
CREATE INDEX outbox_qid ON outbox (qid) WHERE qid IS NOT NULL;
CREATE INDEX stt_inputs_order ON stt_inputs (prio, n, at);

-- A new file's one row of meta, made when it is first opened: the epoch, 16 hex characters of its own
-- (secrets.token_hex(8)). Never this value, and never copied from another file:
--   INSERT INTO meta (key, value) VALUES ('epoch', '0123456789abcdef');
