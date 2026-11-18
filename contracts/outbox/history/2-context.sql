-- The outbox file from #419 on: sizeup/cloud/outbox.py at 7707058c (on main from 2026-11-12 until #671). The audio of
-- another P25 system's clips ("context") has its own budget: a column `context` on outbox, added to an older file when
-- it is opened. Parts and parameters as in 1-events.sql. "-- if-missing: <table>.<column>" before a statement: that
-- build ran it only on a file without that column (it looked with PRAGMA table_info).

-- == open
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    audio BLOB,
    audio_dropped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
PRAGMA table_info(outbox);
-- if-missing: outbox.context
ALTER TABLE outbox ADD COLUMN context INTEGER NOT NULL DEFAULT 0;
SELECT value FROM meta WHERE key = 'epoch';
SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox;
SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox WHERE context = 1;

-- == new file
-- params: ["0123456789abcdef"]
INSERT INTO meta (key, value) VALUES ('epoch', ?);

-- == statements
-- add()
-- params: [1794990000.0, "traffic", "{\"id\":\"east-7001-1794990000\",\"source\":\"East Dispatch\",\"system\":\"east\",\"text\":\"\"}", {"hex": "b2b2b2b2b2b2b2b2b2b2b2b2"}, 1]
INSERT INTO outbox (at, kind, data, audio, context) VALUES (?, ?, ?, ?, ?);
SELECT seq, LENGTH(audio) FROM outbox WHERE audio IS NOT NULL AND context = 1 ORDER BY seq LIMIT 20;
SELECT seq, LENGTH(audio) FROM outbox WHERE audio IS NOT NULL ORDER BY seq LIMIT 20;
-- params: [1]
UPDATE outbox SET audio = NULL, audio_dropped = 1 WHERE seq = ?;
-- batch()
-- params: [200]
SELECT seq, at, kind, data, audio, audio_dropped FROM outbox ORDER BY seq LIMIT ?;
-- ack()
-- params: [1]
DELETE FROM outbox WHERE seq <= ?;
SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox;
SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox WHERE context = 1;
-- pending()
SELECT COUNT(*), MIN(at) FROM outbox;
