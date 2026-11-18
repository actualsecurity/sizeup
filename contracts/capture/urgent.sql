PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;            -- every commit is fsync'd
PRAGMA wal_autocheckpoint = 0;        -- checkpoints are explicit: never on an alarm's insert
CREATE TABLE IF NOT EXISTS urgent (
  n          INTEGER PRIMARY KEY AUTOINCREMENT,
  key        TEXT    NOT NULL,        -- the idempotency key, above
  rev        INTEGER NOT NULL DEFAULT 0,
  kind       TEXT    NOT NULL,        -- 'alarm' | 'page'; a reader leaves an unknown kind alone
  at         REAL    NOT NULL,        -- wall clock when capture made the item, epoch s
  at_boot    REAL    NOT NULL,        -- the same moment on CLOCK_BOOTTIME, s
  event_at   REAL    NOT NULL,        -- when it happened on the air, wall clock, epoch s
  event_boot REAL    NOT NULL,        -- the same moment on CLOCK_BOOTTIME, s
  boot       TEXT    NOT NULL,        -- the boot id at_boot and event_boot belong to
  data       TEXT    NOT NULL,        -- JSON object, the item's data (below)
  state      TEXT    NOT NULL DEFAULT 'new',   -- 'new' | 'sent' | 'app' | 'rejected' | 'expired' (pages only)
  sent_at    REAL,                    -- when the hub answered for it, wall clock, epoch s
  sent_boot  REAL,                    -- the same moment on CLOCK_BOOTTIME, s
  sent_boot_id TEXT,                  -- the boot id sent_boot belongs to
  hub        TEXT,                    -- the hub's word: 'applied' | 'duplicate' | 'stale' | 'rejected'
  hub_gen    TEXT,                    -- the hub generation that answered, as decimal digits (the resend rule)
  tries      INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,                    -- a status and a reason, never a body, never a token
  lost_gen   TEXT,                    -- the hub gen this alarm may have been lost under and was not re-sent (over the cap)
  UNIQUE (key, rev)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  -- 'v' = 1, 'urgent_epoch' = 16 hex; the resend rule's: 'gen', 'gen_seen_at', 'gen_seen_boot', 'gen_seen_boot_id'
CREATE TABLE IF NOT EXISTS p25_bit (             -- #775: when each radio's emergency bit was last seen set
  system TEXT NOT NULL, radio TEXT NOT NULL,
  last_set REAL NOT NULL,                        -- wall clock, epoch s
  last_set_boot REAL NOT NULL, boot TEXT NOT NULL,   -- the same moment on CLOCK_BOOTTIME, and its boot id
  PRIMARY KEY (system, radio)
);
CREATE TABLE IF NOT EXISTS requests (            -- socket request ids: one reply per id, across a restart (§4.2)
  id TEXT PRIMARY KEY, op TEXT NOT NULL,
  reply TEXT,                                    -- the reply's JSON; NULL while the request is being applied
  at REAL NOT NULL, at_boot REAL NOT NULL, boot TEXT NOT NULL
);
