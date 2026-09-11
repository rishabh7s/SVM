-- Baseline DDL for the headless ingestion/triage pipeline.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS captures (
    id            TEXT PRIMARY KEY,
    captured_at   TEXT,
    foreground_app TEXT,
    source_type   TEXT,
    content       TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    node_type   TEXT,
    label       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS edges (
    id          TEXT PRIMARY KEY,
    source_id   TEXT REFERENCES nodes(id),
    target_id   TEXT REFERENCES nodes(id),
    edge_type   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS facts (
    id          TEXT PRIMARY KEY,
    source_id   TEXT REFERENCES captures(id),
    entity      TEXT NOT NULL,
    attribute   TEXT NOT NULL,
    value       TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS episodic_memories (
    id               TEXT PRIMARY KEY,
    source_id        TEXT REFERENCES captures(id),
    event_summary    TEXT NOT NULL,
    temporal_anchor  TEXT,
    actors           TEXT,   -- JSON-encoded list
    outcome          TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS preferences (
    id          TEXT PRIMARY KEY,
    source_id   TEXT REFERENCES captures(id),
    subject     TEXT NOT NULL,
    preference  TEXT NOT NULL,
    context     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decision_logs (
    id                TEXT PRIMARY KEY,
    capture_id        TEXT REFERENCES captures(id),
    original_content  TEXT,
    decision          TEXT NOT NULL CHECK (decision IN ('memorized', 'rejected')),
    reason            TEXT,
    facts_count       INTEGER NOT NULL DEFAULT 0,
    episodes_count    INTEGER NOT NULL DEFAULT 0,
    preferences_count INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_facts_source ON facts(source_id);
CREATE INDEX IF NOT EXISTS idx_episodic_source ON episodic_memories(source_id);
CREATE INDEX IF NOT EXISTS idx_preferences_source ON preferences(source_id);
CREATE INDEX IF NOT EXISTS idx_decision_capture ON decision_logs(capture_id);
