-- Kivi semantic memory schema (SQLite)
-- Mirrors kivi_extraction_schema_v2.json: facts / events / commitments / relationships
-- as generic primitives. entity_type, event_type, and relationship_type are
-- deliberately open TEXT columns, not CHECK-constrained enums, per the
-- generalization decision -- the extractor should not be boxed into a fixed
-- vocabulary designed around one workflow.

PRAGMA foreign_keys = ON;

-- ============================================================
-- Raw captures: one row per ingested record, unmodified, ever.
-- 'manual_edit' is a third modality (alongside speech/selected_text) for
-- captures synthesized when a user edits or corrects a memory directly
-- through the UI or a REST call, rather than through dictation -- keeping
-- this as its own honest modality rather than mislabeling a manual edit as
-- 'selected_text' preserves accurate provenance for that memory going
-- forward (see kivi/api/memory_ops.py).
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
    deleted_at                  TEXT,                    -- NULL unless explicitly deleted; distinct from
                                                            -- supersession -- see module docstring above idx below
    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Hard database-level enforcement of supersession discipline: at most one
-- ACTIVE fact per (entity_id, attribute) at any time. Inserting a new active
-- fact without first deactivating the old one raises an integrity error --
-- this is deliberate; it forces the ingestion code to go through proper
-- supersession logic rather than silently accumulating duplicates.
--
-- deleted_at is orthogonal to is_active/superseded_by_id: a fact can be
-- is_active=1 (still the current pointer in the supersession chain) AND
-- deleted_at set (a user explicitly asked to forget it) at the same time --
-- that combination means "the current value has been deleted, and nothing
-- has superseded it since." The row is never actually removed from the
-- table, so the full audit trail (what it said, when, and that it was later
-- deleted) stays inspectable via get_node_history even after deletion.
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
-- Commitments: anything planned, promised, or owed.
--
-- Split into a stable identity table (commitments) and a versioned status
-- table (commitment_status_events), mirroring the entities/declarative_facts
-- pattern exactly. A commitment's status is NEVER mutated in place -- a
-- status change always inserts a new commitment_status_events row and
-- deactivates the previous one, the same way a new declarative_facts row
-- deactivates the fact it supersedes. This preserves "what did I originally
-- think the status of this was" for free, and makes commitment status
-- auditable the same way fact history already is.
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

-- Hard database-level enforcement of the exact same supersession discipline
-- used for declarative_facts: at most one ACTIVE status per commitment at
-- any time. Inserting a new active status without first deactivating the
-- old one raises an integrity error.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_status_per_commitment
    ON commitment_status_events (commitment_id)
    WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS idx_commitment_status_commitment ON commitment_status_events (commitment_id);
CREATE INDEX IF NOT EXISTS idx_commitment_status_source_capture ON commitment_status_events (source_capture_id);

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

-- NOTE: relationships reference commitments.commitment_id (the stable
-- identity), not a specific commitment_status_events row -- an ordering or
-- resolves relationship is about the commitment itself, not a snapshot of
-- its status at one point in time.

-- ============================================================
-- Unified search index: a single FTS5 table spanning declarative_facts,
-- episodic_events, and commitments, with Porter-stemmed tokenization.
--
-- Why this exists (replacing the earlier hand-rolled Python prefix/stopword
-- heuristic in kivi/text_match.py for this specific tool): that heuristic
-- worked, but it was a stopgap -- Porter stemming is the standard, correct
-- solution to "convergence" vs "converging" and every other word-form
-- mismatch, and SQLite provides it natively via tokenize='porter', so there
-- is no reason to keep re-deriving an approximation of it in Python for
-- this table search. kivi/text_match.py is left as-is for its other use
-- (entity alias fuzzy matching, and the standalone find_resolving_event /
-- get_ordering tools), which don't go through this index.
--
-- Each row's `content` column concatenates THREE things that used to be
-- searched in isolation, one per tool: the entity's canonical_name, the
-- item's own core text (attribute+value / description / commitment_mention),
-- and the capture's foreground_app. A query mentioning any combination of
-- these (an entity name, a domain term, an app name) now matches in one
-- MATCH call instead of requiring the caller to already know which single
-- column to search.
--
-- Rows are inserted once, at creation, and never updated or deleted --
-- supersession/status changes (is_active flips, new commitment_status_events
-- rows) don't change the underlying text, only a flag on the source table,
-- so the index entry for the original row stays valid. Currency (never
-- serving a superseded fact, always joining current commitment status) is
-- guaranteed by ALWAYS re-resolving through the source tables after an FTS
-- match, never by trusting the matched row directly -- see
-- kivi/retrieval/tools.py (get_node_details / get_node_history for the
-- traversal logic that keeps this guarantee under the atomic-tools design).
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS unified_search USING fts5(
    content,
    entity_id UNINDEXED,
    source_type UNINDEXED,
    source_id UNINDEXED,
    tokenize = 'porter'
);

-- Entities are indexed too, as their own searchable node type -- the
-- atomic-tools design needs search_nodes() to be able to return an entity
-- itself as a starting point (e.g. a bare query like "Meridian"), not just
-- facts/events/commitments that happen to mention one. Both the canonical
-- name AND every alias get their own row, since an alias may be the only
-- text form a user's actual query resembles.
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
