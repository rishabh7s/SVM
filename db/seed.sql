-- Hand-written seed data. Small and readable on purpose -- this exists to
-- sanity-check the schema by hand before any pipeline or LLM code touches it.
-- Narrative: work on the "Meridian project" -- a budget gets revised
-- (supersession), a Simulink convergence bug gets fixed (problem/resolution
-- relationship), two commitments are sequenced (must_precede relationship),
-- one commitment gets explicitly confirmed done, and one capture is
-- deliberately quarantined (pii_detected) to exercise the discard path.

PRAGMA foreign_keys = ON;

-- ============================================================
-- Captures
-- ============================================================
INSERT INTO captures (capture_id, raw_asr_text, formatted_text, source_modality, foreground_app, window_title, captured_at, extraction_status, discard_reason, ingested_at) VALUES
('cap_001', 'the meridian project budget ceiling is fifty lakh',
            'The Meridian project budget ceiling is ₹50,00,000.',
            'speech', 'Slack', 'Meridian - #general', '2026-08-10T10:00:00', 'processed', NULL, '2026-08-10T10:00:05'),

('cap_002', 'actually revising meridian budget down to forty five lakh after client pushback',
            'Revising the Meridian budget down to ₹45,00,000 after client pushback.',
            'speech', 'Slack', 'Meridian - #general', '2026-08-20T14:30:00', 'processed', NULL, '2026-08-20T14:30:05'),

('cap_003', 'simulink model not converging looks like a sample time mismatch',
            'Simulink model not converging -- looks like a sample time mismatch between the controller and plant blocks.',
            'speech', 'MATLAB', 'controller_model.slx - Simulink', '2026-08-15T09:00:00', 'processed', NULL, '2026-08-15T09:00:05'),

('cap_004', 'fixed it forced fixed step solver at point zero zero one seconds to match controller sample time',
            'Fixed it -- forced the fixed-step solver at 0.001s to match the controller sample time.',
            'speech', 'MATLAB', 'controller_model.slx - Simulink', '2026-08-15T11:20:00', 'processed', NULL, '2026-08-15T11:20:05'),

('cap_005', 'need to ask the finance lead about the revised budget before committing to a timeline',
            'Need to ask the finance lead about the revised budget before committing to a timeline.',
            'speech', 'Slack', 'Meridian - #general', '2026-08-21T09:15:00', 'processed', NULL, '2026-08-21T09:15:05'),

('cap_006', 'commit to a rollout timeline for meridian once budget is confirmed',
            'Commit to a rollout timeline for Meridian once the budget is confirmed.',
            'speech', 'Slack', 'Meridian - #general', '2026-08-21T09:16:00', 'processed', NULL, '2026-08-21T09:16:05'),

('cap_007', 'talked to the finance lead got confirmation on the forty five lakh number that task is done',
            'Talked to the finance lead, got confirmation on the ₹45,00,000 number. That task is done.',
            'speech', 'Slack', 'Meridian - #general', '2026-08-25T16:00:00', 'processed', NULL, '2026-08-25T16:00:05'),

('cap_008', 'my otp is four seven two nine one okay entering it now also remind me to call the vendor tomorrow',
            'My OTP is 47291, entering it now. Also remind me to call the vendor tomorrow.',
            'speech', 'Chrome', 'Vendor Portal - Login', '2026-08-22T12:00:00', 'pii_detected',
            'OTP detected in dictation; capture quarantined, no facts/events/commitments extracted', '2026-08-22T12:00:05');

-- ============================================================
-- Entities
-- ============================================================
INSERT INTO entities (entity_id, entity_type, canonical_name, created_at) VALUES
('ent_meridian',     'project', 'Meridian project',              '2026-08-10T10:00:05'),
('ent_simulink',     'system',  'Simulink controller model',     '2026-08-15T09:00:05'),
('ent_finance_lead', 'person',  'Finance lead',                  '2026-08-21T09:15:05');

-- ============================================================
-- Entity aliases (entity_search FTS5 rows populate automatically via trigger)
-- ============================================================
INSERT INTO entity_aliases (alias, entity_id, source_capture_id, created_at) VALUES
('meridian project', 'ent_meridian',     'cap_001', '2026-08-10T10:00:05'),
('meridian',          'ent_meridian',     'cap_002', '2026-08-20T14:30:05'),
('simulink model',    'ent_simulink',     'cap_003', '2026-08-15T09:00:05'),
('controller model',  'ent_simulink',     'cap_004', '2026-08-15T11:20:05'),
('finance lead',      'ent_finance_lead', 'cap_005', '2026-08-21T09:15:05');

-- ============================================================
-- Declarative facts -- supersession example.
-- fact_002 (the newer value) must exist before fact_001 can reference it
-- as superseded_by_id, so it's inserted first.
-- ============================================================
INSERT INTO declarative_facts (fact_id, entity_id, attribute, value_text, value_numeric, unit, precision_class, asserter_role, relative_time_expression, resolved_time, source_capture_id, is_active, superseded_by_id, created_at) VALUES
('fact_002', 'ent_meridian', 'budget', NULL, 4500000, 'INR', 'exact_source', 'self', NULL, NULL, 'cap_002', 1, NULL, '2026-08-20T14:30:05');

INSERT INTO declarative_facts (fact_id, entity_id, attribute, value_text, value_numeric, unit, precision_class, asserter_role, relative_time_expression, resolved_time, source_capture_id, is_active, superseded_by_id, created_at) VALUES
('fact_001', 'ent_meridian', 'budget', NULL, 5000000, 'INR', 'exact_source', 'self', NULL, NULL, 'cap_001', 0, 'fact_002', '2026-08-10T10:00:05');

-- ============================================================
-- Episodic events -- problem / resolution pair
-- ============================================================
INSERT INTO episodic_events (event_id, entity_id, event_type, description, relative_time_expression, resolved_time, asserter_role, source_capture_id, created_at) VALUES
('evt_001', 'ent_simulink', 'problem_encountered',
            'Model not converging due to a sample time mismatch between the controller block and the plant block.',
            NULL, NULL, 'self', 'cap_003', '2026-08-15T09:00:05'),
('evt_002', 'ent_simulink', 'resolution_found',
            'Forced the fixed-step solver at 0.001s to match the controller sample time.',
            NULL, NULL, 'self', 'cap_004', '2026-08-15T11:20:05');

-- ============================================================
-- Commitments -- stable identity rows only. Status lives entirely in
-- commitment_status_events below, never here.
-- ============================================================
INSERT INTO commitments (commitment_id, commitment_mention, description, entity_id, source_capture_id, created_at) VALUES
('com_001', 'ask about budget',
            'Ask the finance lead about the revised budget before committing to a timeline.',
            'ent_meridian', 'cap_005', '2026-08-21T09:15:05'),
('com_002', 'commit to timeline',
            'Commit to a Meridian rollout timeline.',
            'ent_meridian', 'cap_006', '2026-08-21T09:16:05');

-- ============================================================
-- Commitment status history -- same supersession pattern as
-- declarative_facts: com_001 opens, then is later superseded by an
-- explicit done-and-confirmed status. com_002 has a single active
-- ('blocked') status with no history yet. As with facts, the newer status
-- row (cse_002) is inserted first so cse_001 can reference it via
-- superseded_by_id.
-- ============================================================
INSERT INTO commitment_status_events (status_event_id, commitment_id, status, status_confirmed_by_user, blocking_reason, due_date_relative_expression, due_date_resolved, source_capture_id, is_active, superseded_by_id, created_at) VALUES
('cse_002', 'com_001', 'done', 1, NULL, NULL, NULL, 'cap_007', 1, NULL, '2026-08-25T16:00:05');

INSERT INTO commitment_status_events (status_event_id, commitment_id, status, status_confirmed_by_user, blocking_reason, due_date_relative_expression, due_date_resolved, source_capture_id, is_active, superseded_by_id, created_at) VALUES
('cse_001', 'com_001', 'open', 0, NULL, NULL, NULL, 'cap_005', 0, 'cse_002', '2026-08-21T09:15:05');

INSERT INTO commitment_status_events (status_event_id, commitment_id, status, status_confirmed_by_user, blocking_reason, due_date_relative_expression, due_date_resolved, source_capture_id, is_active, superseded_by_id, created_at) VALUES
('cse_003', 'com_002', 'blocked', 0, 'waiting on budget confirmation from the finance lead', NULL, NULL, 'cap_006', 1, NULL, '2026-08-21T09:16:05');

-- ============================================================
-- Relationships -- solution reuse (resolves) and ordering (must_precede)
-- ============================================================
INSERT INTO relationships (relationship_id, source_type, source_id, target_type, target_id, relationship_type, reason, source_capture_id, created_at) VALUES
('rel_001', 'event', 'evt_001', 'event', 'evt_002', 'resolves', NULL, 'cap_004', '2026-08-15T11:20:05'),
('rel_002', 'commitment', 'com_001', 'commitment', 'com_002', 'must_precede',
            'need the budget answer before committing to the timeline', 'cap_006', '2026-08-21T09:16:05');
