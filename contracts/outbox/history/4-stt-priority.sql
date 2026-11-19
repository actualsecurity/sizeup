-- The outbox file from #701 on: sizeup/cloud/outbox.py at 93c7658c (on main from 2026-11-17 until #762 on 2026-11-18).
-- Each speech-to-text input has a priority (prio, added to an older file when it is opened) and the lane is read in
-- its order through an index, stt_inputs_order. The file as it was the day before the quarantine: no quarantine and
-- source_latest tables, no outbox.qid, no outbox_qid index. Parts and parameters as in 1-events.sql to 3-stt-inputs.sql.

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
    meta TEXT NOT NULL,
    prio INTEGER NOT NULL DEFAULT 1
);
PRAGMA table_info(outbox);
-- if-missing: outbox.context
ALTER TABLE outbox ADD COLUMN context INTEGER NOT NULL DEFAULT 0;
PRAGMA table_info(stt_inputs);
-- if-missing: stt_inputs.prio
ALTER TABLE stt_inputs ADD COLUMN prio INTEGER NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS stt_inputs_order ON stt_inputs (prio, n, at);
SELECT value FROM meta WHERE key = 'epoch';
SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox;
SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox WHERE context = 1;
SELECT COALESCE(SUM(LENGTH(flac)), 0) FROM stt_inputs;

-- == new file
-- params: ["0123456789abcdef"]
INSERT INTO meta (key, value) VALUES ('epoch', ?);

-- == statements
-- add_stt()
-- params: [1794990000.0, "{\"id\":\"a1b2c3d4e5\",\"kind\":\"page\",\"label\":\"Paging 1\",\"end\":1794990000.0}", {"hex": "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"}, "{\"v\":1,\"format\":\"flac\",\"input\":\"stt-src\",\"kind\":\"page\",\"rate\":16000,\"samples\":16000,\"voice_offset_s\":0.5}", 3]
INSERT INTO stt_inputs (at, data, flac, meta, prio) VALUES (?, ?, ?, ?, ?);
-- _drop_stt()
SELECT n, prio FROM stt_inputs INDEXED BY stt_inputs_order ORDER BY prio, n LIMIT 20;
-- params: [1]
SELECT LENGTH(flac) FROM stt_inputs WHERE n = ?;
-- params: [1]
DELETE FROM stt_inputs WHERE n = ?;
-- stt_batch()  (_stt_order: the priorities, then pre-alert and fire fresh within 300 s first, then the rest)
SELECT DISTINCT prio FROM stt_inputs INDEXED BY stt_inputs_order ORDER BY prio DESC;
-- params: [3, 1794989700.0, 50]
SELECT n FROM stt_inputs INDEXED BY stt_inputs_order WHERE prio = ? AND at >= ? ORDER BY n LIMIT ?;
-- params: [3, 1794989700.0, 50]
SELECT n FROM stt_inputs INDEXED BY stt_inputs_order WHERE prio = ? AND at < ? ORDER BY n LIMIT ?;
-- params: [1, 50]
SELECT n FROM stt_inputs INDEXED BY stt_inputs_order WHERE prio = ? ORDER BY n LIMIT ?;
-- params: [2]
SELECT at, data, flac, meta, prio FROM stt_inputs WHERE n = ?;
-- stt_pending()
SELECT COUNT(*) FROM stt_inputs INDEXED BY stt_inputs_order;
-- stt_ack()  (one mark for each n)
-- params: [1, 2]
SELECT COALESCE(SUM(LENGTH(flac)), 0) FROM stt_inputs WHERE n IN (?,?);
-- params: [1, 2]
DELETE FROM stt_inputs WHERE n IN (?,?);
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
