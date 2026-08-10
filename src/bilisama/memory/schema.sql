-- One SQLite file per room (plan section 4.7): WAL, stdlib sqlite3, no ORM,
-- no vectors. Wall timestamps are ISO-8601 UTC strings from the injected
-- clock, so replayed fixtures produce byte-identical rows.

CREATE TABLE IF NOT EXISTS stream (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    identity TEXT NOT NULL,             -- Viewer.identity, never empty
    uname TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    value_cny REAL NOT NULL DEFAULT 0,
    wall_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_stream ON event(stream_id);
CREATE INDEX IF NOT EXISTS event_wall ON event(wall_at);

-- Viewer rows are never deleted (plan section 4.7): they are small, and they
-- are the entire point. streams_seen bumps once per stream via last_stream_id.
CREATE TABLE IF NOT EXISTS viewer (
    identity TEXT PRIMARY KEY,
    uid INTEGER NOT NULL DEFAULT 0,
    uid_hash TEXT NOT NULL DEFAULT '',
    uname TEXT NOT NULL DEFAULT '',
    guard_level TEXT NOT NULL DEFAULT 'none',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    streams_seen INTEGER NOT NULL DEFAULT 0,
    last_stream_id INTEGER NOT NULL DEFAULT 0,
    msg_count INTEGER NOT NULL DEFAULT 0,
    gift_value_cny REAL NOT NULL DEFAULT 0
);

-- scope: 'streamer' | 'viewer' | 'stream'. Replacement is delete-then-insert
-- on (scope, subject), which keeps the table bounded by construction.
CREATE TABLE IF NOT EXISTS fact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fact_scope ON fact(scope, subject);
