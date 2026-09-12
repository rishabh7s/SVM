# Kivi evaluation -- 2026-09-11T22:33:25+00:00

- mode: **offline (retrieval checks only)**
- database: `C:\Users\Rishabh Singh\Downloads\SVM_claude\evals\results\20260912_040325\eval.db`
- eval set: `C:\Users\Rishabh Singh\Downloads\SVM_claude\evals\eval_set.json` (14 cases)

## Results

- retrieval checks: **4/14 passed**

## Failures (kept visible on purpose)

### current_owner_after_handover -- Who currently owns Project Driftwood?
- why it matters: Driftwood changed hands. The superseded owner's row is still in the FTS index forever, so quoting it is the single easiest way for this system to be confidently wrong.
- retrieval check `entity_fact` failed: entity 'Project Driftwood' not found in this database
- retrieval check `no_duplicate_attribute` failed: entity 'Project Driftwood' not found

### numeric_fact_readable -- What is the budget for project Jinangxi?
- why it matters: The value lives in value_numeric with value_text NULL. Every display path has to reunite those columns; the one that didn't rendered this fact as 'None'.
- retrieval check `entity_fact` failed: entity 'project Jinangxi' not found in this database

### compound_two_preferences -- What are my preferences for focus blocks and for meeting note formatting?
- why it matters: Two unrelated topics in one turn. The reported failure was searching the first, finding something, and answering as though the second half of the sentence wasn't there.
- retrieval check `search_finds` failed: 0 matching result(s)

### compound_two_entities -- What's the deadline for Project Driftwood, and who owns the Helix migration?
- why it matters: Two entities, two different attributes, one turn. Tests decomposition across entities rather than across topics.
- retrieval check `entity_fact` failed: entity 'Project Driftwood' not found in this database
- retrieval check `search_finds` failed: 0 matching result(s)

### history_available_on_request -- What was Project Driftwood's owner before the current one?
- why it matters: Superseded values are hidden from ordinary retrieval but must remain reachable when the question is explicitly about history -- otherwise 'never show stale data' has quietly become 'destroy the audit trail'. The citation here SHOULD be the superseded row -- that is the record being asked about.
- retrieval check `search_finds` failed: 0 matching result(s)

### preference_recall -- How do I like my commit messages written?
- why it matters: Preference-level recall, phrased nothing like the stored text. Tests that the preference layer is retrievable by intent, not just by matching words.
- retrieval check `search_finds` failed: 0 matching result(s)

### distributed_across_dictations -- Tell me everything currently true about the Helix migration.
- why it matters: The answer is spread across several dictations recorded weeks apart. This is the case that proves memory is doing something a single transcript search cannot.
- retrieval check `no_duplicate_attribute` failed: entity 'Helix migration' not found

### false_premise_is_corrected -- Since the Driftwood deadline was already agreed and signed off, what's the next step?
- why it matters: The question smuggles in an assumption. Accepting a premise the history doesn't support is a subtler kind of hallucination than inventing a fact, and a more damaging one.
- retrieval check `entity_fact` failed: entity 'Project Driftwood' not found in this database

## Database

| metric | before | after |
|---|---:|---:|
| active_facts | 1 | 1 |
| captures | 8 | 8 |
| commitment_status_events | 3 | 3 |
| commitments | 2 | 2 |
| decision_logs | 0 | 0 |
| declarative_facts | 2 | 2 |
| entities | 3 | 3 |
| episodic_events | 2 | 2 |
| preferences | 3 | 3 |
| rejected_captures | 1 | 1 |
| relationships | 2 | 2 |
| size_bytes | 204800 | 204800 |
| superseded_facts | 1 | 1 |

## Ingestion decisions

- decisions: `{}`
- latency (ms): `{'count': 0, 'mean': None, 'median': None, 'p95': None, 'max': None}`

What ingestion deliberately ignored (sample):

- `cap_008` -- pii_detected: OTP detected in dictation; capture quarantined, no facts/events/commitments extracted

## Performance and usage

- agent_model_calls: `0`
- prompt_tokens: `None`
- completion_tokens: `None`
- total_tokens: `None`
- estimated_cost_usd: `not configured -- set KIVI_EVAL_COST_PER_1M_INPUT / _OUTPUT to price a run`
- retrieval_check_latency_ms_mean: `0.31`
- agent_latency_ms_mean: `None`
- agent_latency_ms_median: `None`
- agent_latency_ms_max: `None`
- ingestion_wall_clock_s: `None`
- db_growth_bytes: `0`
