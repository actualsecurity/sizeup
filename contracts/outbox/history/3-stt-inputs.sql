-- The outbox file from #671 on: sizeup/cloud/outbox.py at 606b2225 (on main 2026-11-17, until #701 the same day). The
-- speech-to-text inputs have a table of their own, stt_inputs, sent oldest first: no priority yet, no index. Parts
-- and parameters as in 1-events.sql and 2-context.sql.
--
-- (Inside #671, never on main: 2e55493a kept the input on the event's own row instead, in three columns it added to
-- outbox when it opened the file: stt BLOB, stt_meta TEXT, stt_dropped INTEGER NOT NULL DEFAULT 0. A file a
-- development station made with that commit still has them; today's code leaves them alone. Tested.)

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
CREATE TABLE IF NOT EXISTS stt_inputs (
    n INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    data TEXT NOT NULL,
    flac BLOB NOT NULL,
    meta TEXT NOT NULL
);
PRAGMA table_info(outbox);
-- if-missing: outbox.context
ALTER TABLE outbox ADD COLUMN context INTEGER NOT NULL DEFAULT 0;
SELECT value FROM meta WHERE key = 'epoch';
SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox;
SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox WHERE context = 1;
SELECT COALESCE(SUM(LENGTH(flac)), 0) FROM stt_inputs;

-- == new file
-- params: ["0123456789abcdef"]
INSERT INTO meta (key, value) VALUES ('epoch', ?);

-- == statements
-- add_stt()
-- params: [1794990000.0, "{\"id\":\"a1b2c3d4e5\",\"kind\":\"page\",\"label\":\"Paging 1\",\"end\":1794990000.0}", {"hex": "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"}, "{\"v\":1,\"format\":\"flac\",\"input\":\"stt-src\",\"kind\":\"page\",\"rate\":16000,\"samples\":16000,\"voice_offset_s\":0.5}"]
INSERT INTO stt_inputs (at, data, flac, meta) VALUES (?, ?, ?, ?);
SELECT n, LENGTH(flac) FROM stt_inputs ORDER BY n LIMIT 20;
-- stt_batch()
-- params: [50]
SELECT n, at, data, flac, meta FROM stt_inputs ORDER BY n LIMIT ?;
-- stt_pending()
SELECT COUNT(*), COALESCE(SUM(LENGTH(flac)), 0) FROM stt_inputs;
-- stt_ack()  (one mark for each n)
-- params: [1, 2]
DELETE FROM stt_inputs WHERE n IN (?,?);
SELECT COALESCE(SUM(LENGTH(flac)), 0) FROM stt_inputs;
-- _drop_stt()
-- params: [1]
DELETE FROM stt_inputs WHERE n = ?;
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
