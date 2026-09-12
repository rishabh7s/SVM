-- Kivi semantic memory schema (SQLite).
--
-- entity_type, attribute, event_type and relationship_type are open TEXT on
-- purpose: a fixed vocabulary designed around one workflow would be wrong
-- for the next user. Drift is managed in the writer, not by closing them.

PRAGMA foreign_keys = ON;

-- ============================================================
-- Raw captures: one row per ingested record, never modified.
--
-- 'manual_edit' is its own modality for captures created by editing memory
-- through the UI or a tool, rather than mislabelling those as dictation.
-- ============================================================
CREATE TABLE IF NOT EXISTS captures (
    capture_id          TEXT PRIMARY KEY,
    raw_asr_text        TEXT,
    formatted_text      TEXT,
    source_modality     TEXT NOT NULL CHECK (source_modality IN ('speech', 'selected_text', 'manual_edit')),
    foreground_app      TEXT,
    window_title        TEXT,
    captured_at         TEXT NOT NULL,          -- ISO-8601; anchor for relative-time resolution
    extraction_status   TEXT NOT NULL DEFAULT 'pending'
                         CHECK (extraction_status IN
                             ('pending', 'processed', 'transient_discard', 'pii_detected', 'incomplete_capture')),
    discard_reason      TEXT,                   -- required (non-null) whenever status != 'processed'
    ingested_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Canonical entities + alias registry (L1/L2 entity resolution)
-- ============================================================
CREATE TABLE IF NOT EXISTS entities (
    entity_id       TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL,              -- open text: 'project','person','system','document', etc.
    canonical_name  TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias               TEXT NOT NULL,
    entity_id           TEXT NOT NULL REFERENCES entities(entity_id),
    source_capture_id   TEXT REFERENCES captures(capture_id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (alias, entity_id)
);

-- FTS5 index over aliases, used for the L2 fuzzy entity-resolution fallback
-- and for solution-reuse / entity search from the retrieval agent.
CREATE VIRTUAL TABLE IF NOT EXISTS entity_search USING fts5(
    entity_id UNINDEXED,
    alias
);

-- Keep entity_search in sync automatically whenever an alias is written,
-- so application code never has to remember to update both tables.
CREATE TRIGGER IF NOT EXISTS trg_entity_aliases_ai
AFTER INSERT ON entity_aliases
BEGIN
    INSERT INTO entity_search (entity_id, alias) VALUES (new.entity_id, new.alias);
END;

-- ============================================================
-- Declarative facts: entity/attribute/value with supersession chain
-- ============================================================
CREATE TABLE IF NOT EXISTS declarative_facts (
    fact_id                     TEXT PRIMARY KEY,
    entity_id                   TEXT NOT NULL REFERENCES entities(entity_id),
    attribute                   TEXT NOT NULL,          -- open text: 'budget','deadline','owner', etc.
    value_text                  TEXT,
    value_numeric               REAL,
    unit                        TEXT,
    precision_class             TEXT NOT NULL CHECK (precision_class IN ('exact_source', 'spoken_approximation')),
    asserter_role               TEXT NOT NULL CHECK (asserter_role IN ('self', 'third_party')),
    relative_time_expression    TEXT,
    resolved_time                TEXT,
    source_capture_id           TEXT NOT NULL REFERENCES captures(capture_id),
    is_active                   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    superseded_by_id            TEXT REFERENCES declarative_facts(fact_id),
    deleted_at                  TEXT,                    -- see the index comment below
    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- At most one ACTIVE fact per (entity_id, attribute), enforced by the
-- database rather than by remembering to check. Inserting a new active fact
-- without deactivating the old one is an integrity error, which forces the
-- writer through proper supersession instead of silently accumulating two
-- current answers.
--
-- Superseded and deleted are told apart by the pair of columns:
--   superseded -> is_active=0, superseded_by_id set,  deleted_at NULL
--   deleted    -> is_active=0, superseded_by_id NULL, deleted_at set
-- A delete clears is_active as well, so every reader that already filters on
-- is_active excludes forgotten memories for free. The row itself is never
-- removed, so get_node_history still shows what it said.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_fact_per_attribute
    ON declarative_facts (entity_id, attribute)
    WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS idx_facts_entity ON declarative_facts (entity_id);
CREATE INDEX IF NOT EXISTS idx_facts_source_capture ON declarative_facts (source_capture_id);

-- ============================================================
-- Episodic events: things that happened
-- ============================================================
CREATE TABLE IF NOT EXISTS episodic_events (
    event_id                    TEXT PRIMARY KEY,
    entity_id                   TEXT REFERENCES entities(entity_id),   -- nullable: some events are self-contained
    event_type                  TEXT NOT NULL,          -- open text: 'problem_encountered','decision', etc.
    description                 TEXT NOT NULL,
    relative_time_expression    TEXT,
    resolved_time                TEXT,
    asserter_role                TEXT CHECK (asserter_role IN ('self', 'third_party') OR asserter_role IS NULL),
    source_capture_id           TEXT NOT NULL REFERENCES captures(capture_id),
    deleted_at                  TEXT,                    -- NULL unless explicitly deleted (soft delete only)
    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_entity ON episodic_events (entity_id);
CREATE INDEX IF NOT EXISTS idx_events_source_capture ON episodic_events (source_capture_id);

-- ============================================================
-- Commitments: anything planned, promised or owed.
--
-- Identity and status are split. A commitments row never changes; every
-- status change inserts a new commitment_status_events row and deactivates
-- the previous one, the same way a new fact deactivates the one it
-- supersedes. "What did I originally think the status was" stays answerable.
-- ============================================================
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id           TEXT PRIMARY KEY,
    commitment_mention      TEXT NOT NULL,
    description              TEXT NOT NULL,
    entity_id                TEXT REFERENCES entities(entity_id),
    source_capture_id        TEXT NOT NULL REFERENCES captures(capture_id),  -- capture that first created this commitment
    deleted_at                TEXT,                     -- NULL unless explicitly deleted (soft delete only)
    created_at                TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_commitments_entity ON commitments (entity_id);

CREATE TABLE IF NOT EXISTS commitment_status_events (
    status_event_id              TEXT PRIMARY KEY,
    commitment_id                TEXT NOT NULL REFERENCES commitments(commitment_id),
    status                       TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'blocked', 'done')),
    status_confirmed_by_user     INTEGER NOT NULL DEFAULT 0 CHECK (status_confirmed_by_user IN (0, 1)),
    blocking_reason               TEXT,
    due_date_relative_expression TEXT,
    due_date_resolved            TEXT,
    source_capture_id             TEXT NOT NULL REFERENCES captures(capture_id),
    is_active                     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    superseded_by_id              TEXT REFERENCES commitment_status_events(status_event_id),
    created_at                     TEXT NOT NULL DEFAULT (datetime('now')),

    -- Database-level enforcement of "never done without explicit confirmation" --
    -- mirrors the JSON Schema's if/then so the guarantee holds even if some
    -- future code path forgets to check it in Python.
    CHECK (status != 'done' OR status_confirmed_by_user = 1)
);

-- Same discipline as facts: at most one active status per commitment.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_status_per_commitment
    ON commitment_status_events (commitment_id)
    WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS idx_commitment_status_commitment ON commitment_status_events (commitment_id);
CREATE INDEX IF NOT EXISTS idx_commitment_status_source_capture ON commitment_status_events (source_capture_id);

-- ============================================================
-- Preferences: how the user wants things done, as opposed to a fact, which
-- is something true about the world. entity_id is nullable because most
-- preferences aren't about any one thing.
--
-- Supersession mirrors declarative_facts but keys on (entity_id, category),
-- and only when both are present. An unscoped preference has no reliable
-- key, so it is appended instead. See writer.py's _write_preference.
-- ============================================================
CREATE TABLE IF NOT EXISTS preferences (
    preference_id      TEXT PRIMARY KEY,
    entity_id           TEXT REFERENCES entities(entity_id),   -- nullable: most preferences aren't entity-scoped
    category             TEXT,                                  -- open text, e.g. 'formatting','workflow'; nullable
    preference_text      TEXT NOT NULL,
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    superseded_by_id      TEXT REFERENCES preferences(preference_id),
    source_capture_id     TEXT NOT NULL REFERENCES captures(capture_id),
    deleted_at             TEXT,                                 -- NULL unless explicitly deleted (soft delete only)
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Only enforceable when both keys are concrete -- a partial index can't
-- treat NULL entity_id as its own group. That's fine: the entity+category
-- case is the only one the writer ever tries to supersede.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_preference_per_entity_category
    ON preferences (entity_id, category)
    WHERE is_active = 1 AND entity_id IS NOT NULL AND category IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_preferences_entity ON preferences (entity_id);
CREATE INDEX IF NOT EXISTS idx_preferences_source_capture ON preferences (source_capture_id);

-- ============================================================
-- One row per capture that finished processing, memorized or rejected. This
-- is what makes "why didn't this become memory?" a query rather than a diff
-- across five tables.
-- ============================================================
CREATE TABLE IF NOT EXISTS decision_logs (
    decision_log_id     TEXT PRIMARY KEY,
    capture_id          TEXT NOT NULL REFERENCES captures(capture_id),
    decision            TEXT NOT NULL CHECK (decision IN ('memorized', 'rejected')),
    reason               TEXT,                 -- discard_reason when rejected; NULL when memorized
    facts_created        INTEGER NOT NULL DEFAULT 0,
    events_created        INTEGER NOT NULL DEFAULT 0,
    commitments_created    INTEGER NOT NULL DEFAULT 0,
    preferences_created     INTEGER NOT NULL DEFAULT 0,
    latency_ms               REAL,             -- wall-clock time for this capture's triage+extraction+write
    created_at                TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_decision_logs_capture ON decision_logs (capture_id);

-- ============================================================
-- Relationships: generic links between any two facts/events/commitments
-- ============================================================
CREATE TABLE IF NOT EXISTS relationships (
    relationship_id     TEXT PRIMARY KEY,
    source_type         TEXT NOT NULL CHECK (source_type IN ('fact', 'event', 'commitment')),
    source_id           TEXT NOT NULL,          -- polymorphic: no DB-level FK possible across three tables
    target_type         TEXT NOT NULL CHECK (target_type IN ('fact', 'event', 'commitment')),
    target_id           TEXT NOT NULL,
    relationship_type   TEXT NOT NULL,          -- open text: 'must_precede','resolves','corrects', etc.
    reason               TEXT,
    source_capture_id   TEXT NOT NULL REFERENCES captures(capture_id),
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships (source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships (target_type, target_id);

-- Relationships point at commitment_id, not at a status row: an ordering
-- edge is about the commitment, not a snapshot of its status.

-- ============================================================
-- One FTS5 table over facts, events, commitments, preferences and entities,
-- Porter-stemmed so 'converging' finds 'convergence'.
--
-- Each row's content concatenates the entity name, the item's own text and
-- the capture's foreground_app, so a query naming any of the three matches
-- in one MATCH instead of needing the caller to pick a column first.
--
-- Rows are inserted once and never updated. Supersession only flips a flag
-- on the source table, so the indexed text stays valid -- but it also means
-- a retired row's text lives here forever. Currency comes from re-resolving
-- through the source tables after a match, never from trusting the hit
-- itself. See kivi/retrieval/tools.py.
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS unified_search USING fts5(
    content,
    entity_id UNINDEXED,
    source_type UNINDEXED,
    source_id UNINDEXED,
    tokenize = 'porter'
);

-- Entities are indexed too, so a bare query like "Meridian" can return the
-- entity itself as a starting point. Aliases get their own rows -- an alias
-- may be the only form the query resembles.
CREATE TRIGGER IF NOT EXISTS trg_index_entity AFTER INSERT ON entities
BEGIN
    INSERT INTO unified_search (content, entity_id, source_type, source_id)
    VALUES (new.canonical_name || ' ' || new.entity_type, new.entity_id, 'entity', new.entity_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_index_entity_alias AFTER INSERT ON entity_aliases
BEGIN
    INSERT INTO unified_search (content, entity_id, source_type, source_id)
    VALUES (new.alias, new.entity_id, 'entity', new.entity_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_index_fact AFTER INSERT ON declarative_facts
BEGIN
    INSERT INTO unified_search (content, entity_id, source_type, source_id)
    SELECT
        COALESCE((SELECT canonical_name FROM entities WHERE entity_id = new.entity_id), '') || ' ' ||
        new.attribute || ' ' || COALESCE(new.value_text, '') || ' ' ||
        COALESCE((SELECT foreground_app FROM captures WHERE capture_id = new.source_capture_id), ''),
        new.entity_id,
        'fact',
        new.fact_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_index_event AFTER INSERT ON episodic_events
BEGIN
    INSERT INTO unified_search (content, entity_id, source_type, source_id)
    SELECT
        COALESCE((SELECT canonical_name FROM entities WHERE entity_id = new.entity_id), '') || ' ' ||
        new.event_type || ' ' || new.description || ' ' ||
        COALESCE((SELECT foreground_app FROM captures WHERE capture_id = new.source_capture_id), ''),
        new.entity_id,
        'event',
        new.event_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_index_commitment AFTER INSERT ON commitments
BEGIN
    INSERT INTO unified_search (content, entity_id, source_type, source_id)
    SELECT
        COALESCE((SELECT canonical_name FROM entities WHERE entity_id = new.entity_id), '') || ' ' ||
        new.commitment_mention || ' ' || new.description || ' ' ||
        COALESCE((SELECT foreground_app FROM captures WHERE capture_id = new.source_capture_id), ''),
        new.entity_id,
        'commitment',
        new.commitment_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_index_preference AFTER INSERT ON preferences
BEGIN
    INSERT INTO unified_search (content, entity_id, source_type, source_id)
    SELECT
        COALESCE((SELECT canonical_name FROM entities WHERE entity_id = new.entity_id), '') || ' ' ||
        COALESCE(new.category, '') || ' ' || new.preference_text || ' ' ||
        COALESCE((SELECT foreground_app FROM captures WHERE capture_id = new.source_capture_id), ''),
        new.entity_id,
        'preference',
        new.preference_id;
END;
