-- The outbox file of the first build that had one: sizeup/cloud/outbox.py at 1c49db24 (#3: on main from 2026-10-07
-- until #419). Events only: no context audio, no speech-to-text inputs.
--
-- Read by tests/test_contracts_outbox.py. Three parts. "open": what that build ran every time it opened the file, so
-- what its file holds. "new file": what it ran once, on a file without an epoch. "statements": everything else it
-- ran, under the method that ran it, each once, with made-up parameters on a "-- params:" line (a JSON list;
-- {"hex": "..."} is a BLOB). A file of today must take every one of them: that is a switch back to this build.

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
SELECT value FROM meta WHERE key = 'epoch';
SELECT COALESCE(SUM(LENGTH(audio)), 0) FROM outbox;

-- == new file
-- params: ["0123456789abcdef"]
INSERT INTO meta (key, value) VALUES ('epoch', ?);

-- == statements
-- add()
-- params: [1794990000.0, "traffic", "{\"id\":\"Fireground1-1794990000000\",\"source\":\"Fireground 1\",\"text\":\"Engine 1 is on scene\"}", {"hex": "a1a1a1a1a1a1a1a1a1a1a1a1"}]
INSERT INTO outbox (at, kind, data, audio) VALUES (?, ?, ?, ?);
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
-- pending()
SELECT COUNT(*), MIN(at) FROM outbox;
