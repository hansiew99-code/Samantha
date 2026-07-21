-- Samantha SQLite schema. Idempotent: applied on every boot.

CREATE TABLE IF NOT EXISTS core_memory (
    key         TEXT PRIMARY KEY,           -- e.g. 'identity', 'preferences', 'standing_rules'
    content     TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS facts (
    id            INTEGER PRIMARY KEY,
    subject       TEXT NOT NULL,
    predicate     TEXT NOT NULL,
    object        TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'chat',    -- chat|email|slack|clickup|calendar|consolidation
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_by INTEGER REFERENCES facts(id),    -- non-null = no longer current (kept forever)
    ref_count     INTEGER NOT NULL DEFAULT 0       -- bumped on retrieval; used by consolidation
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    subject, predicate, object,
    content='facts', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, subject, predicate, object)
    VALUES (new.id, new.subject, new.predicate, new.object);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, predicate, object)
    VALUES ('delete', old.id, old.subject, old.predicate, old.object);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, predicate, object)
    VALUES ('delete', old.id, old.subject, old.predicate, old.object);
    INSERT INTO facts_fts(rowid, subject, predicate, object)
    VALUES (new.id, new.subject, new.predicate, new.object);
END;

CREATE TABLE IF NOT EXISTS people (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    email        TEXT,
    relationship TEXT,
    timezone     TEXT,
    notes        TEXT,
    vip          INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,                       -- pre-composed at creation; firing costs 0 tokens
    due_at     TEXT NOT NULL,                       -- ISO-8601 UTC
    recurrence TEXT,                                -- NULL = one-shot; else cron expression
    status     TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled|fired|done|snoozed|cancelled
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,                       -- clickup|gmail|slack|calendar|*
    action     TEXT NOT NULL,                       -- suppress|vip|ignore_sender|...
    scope      TEXT NOT NULL DEFAULT '*',           -- channel name, sender, task list, or *
    detail     TEXT,                                -- JSON
    active     INTEGER NOT NULL DEFAULT 1,          -- deactivated rules are kept, never deleted
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    role       TEXT NOT NULL,                       -- user|assistant|system
    content    TEXT NOT NULL,
    channel    TEXT NOT NULL DEFAULT 'telegram',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL DEFAULT 'local',      -- local|clickup
    external_id TEXT,
    title       TEXT NOT NULL,
    due_at      TEXT,
    status      TEXT NOT NULL DEFAULT 'open',       -- open|done
    data        TEXT,                               -- JSON blob from the source system
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,                      -- gmail_send|slack_send
    payload     TEXT NOT NULL,                      -- JSON needed to execute
    preview     TEXT NOT NULL,                      -- what the user sees in Telegram
    status      TEXT NOT NULL DEFAULT 'pending',    -- pending|sent|discarded|failed
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS events_queue (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,                     -- gmail|slack|clickup|calendar|scheduler
    kind         TEXT NOT NULL,                     -- new_email|mention|due_soon|conflict|...
    scope        TEXT NOT NULL DEFAULT '*',         -- sender/channel/list — matched against rules
    payload      TEXT NOT NULL,                     -- JSON
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS spend_log (
    id                INTEGER PRIMARY KEY,
    model             TEXT NOT NULL,
    purpose           TEXT NOT NULL,                -- chat|escalation|sweep|digest|consolidation
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    batch             INTEGER NOT NULL DEFAULT 0,   -- 1 = Batch API (50% off)
    cost_usd          REAL NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_spend_created ON spend_log(created_at);
CREATE INDEX IF NOT EXISTS idx_facts_current ON facts(superseded_by) WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_events_unprocessed ON events_queue(processed_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status);

CREATE TABLE IF NOT EXISTS integration_state (
    key        TEXT PRIMARY KEY,                    -- e.g. 'gmail.history_id', 'gcal.sync_token'
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
