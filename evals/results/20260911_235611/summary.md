# Kivi evaluation -- 2026-09-11T18:26:11+00:00

- mode: **full pipeline (agent model: gemini-3.1-flash-lite)**
- database: `C:\Users\Rishabh Singh\Downloads\SVM_claude\evals\results\20260911_235611\eval.db`
- eval set: `C:\Users\Rishabh Singh\Downloads\SVM_claude\evals\eval_set.json` (12 cases)

## Results

- retrieval checks: **14/14 passed**
- agent cases: **2/12 passed**
- post-run state checks: **0/1 passed**

## Failures (kept visible on purpose)

### current_owner_after_handover -- Who currently owns Project Driftwood?
- why it matters: Driftwood changed hands. The superseded owner's row is still in the FTS index forever, so quoting it is the single easiest way for this system to be confidently wrong.
- agent: expected response_type='answer', got 'abstain'
- agent: answer does not mention 'Sofia'
- agent: answer carries no citation

### numeric_fact_readable -- What is the budget for project Jinangxi?
- why it matters: The value lives in value_numeric with value_text NULL. Every display path has to reunite those columns; the one that didn't rendered this fact as 'None'.
- agent: expected response_type='answer', got 'abstain'
- agent: answer does not mention '40'
- agent: answer carries no citation

### compound_two_preferences -- What are my preferences for focus blocks and for meeting note formatting?
- why it matters: Two unrelated topics in one turn. The reported failure was searching the first, finding something, and answering as though the second half of the sentence wasn't there.
- agent: expected response_type='answer', got 'abstain'
- agent: answer does not mention 'focus block'
- agent: answer does not mention 'bullet'
- agent: answer carries no citation

### compound_two_entities -- What's the deadline for Project Driftwood, and who owns the Helix migration?
- why it matters: Two entities, two different attributes, one turn. Tests decomposition across entities rather than across topics.
- agent: expected response_type='answer', got 'abstain'
- agent: answer carries no citation

### conversational_deletion_persists -- Forget the budget for project Jinangxi.
- why it matters: The headline failure: the agent said 'I have forgotten that' and the row kept is_active=1, deleted_at=NULL. The post_check is the whole point of this case -- a passing answer with a failing post_check is exactly the bug.
- agent: delete_memory was never called (called: none)
- post-check `fact_deleted` failed: the agent's reply claimed a deletion the database did not record

### commitment_status_is_respected -- What am I still waiting on for Project Driftwood?
- why it matters: Completed and cancelled commitments must not be presented as outstanding work. A memory system that reminds you about things you already finished stops being trusted quickly.
- agent: expected response_type='answer', got 'abstain'
- agent: answer carries no citation

### history_available_on_request -- What was Project Driftwood's owner before the current one?
- why it matters: Superseded values are hidden from ordinary retrieval but must remain reachable when the question is explicitly about history -- otherwise 'never show stale data' has quietly become 'destroy the audit trail'.
- agent: expected response_type='answer', got 'abstain'
- agent: answer carries no citation

### preference_recall -- How do I like my commit messages written?
- why it matters: Preference-level recall, phrased nothing like the stored text. Tests that the preference layer is retrievable by intent, not just by matching words.
- agent: expected response_type='answer', got 'abstain'
- agent: answer does not mention 'imperative'
- agent: answer carries no citation

### distributed_across_dictations -- Tell me everything currently true about the Helix migration.
- why it matters: The answer is spread across several dictations recorded weeks apart. This is the case that proves memory is doing something a single transcript search cannot.
- agent: expected response_type='answer', got 'abstain'
- agent: answer carries no citation

### false_premise_is_corrected -- Since the Driftwood deadline was already agreed and signed off, what's the next step?
- why it matters: The question smuggles in an assumption. Accepting a premise the history doesn't support is a subtler kind of hallucination than inventing a fact, and a more damaging one.
- agent: answer carries no citation

## Database

| metric | before | after |
|---|---:|---:|
| active_facts | 192 | 192 |
| captures | 505 | 505 |
| commitment_status_events | 152 | 152 |
| commitments | 112 | 112 |
| decision_logs | 504 | 504 |
| declarative_facts | 282 | 282 |
| entities | 50 | 50 |
| episodic_events | 148 | 148 |
| preferences | 39 | 39 |
| rejected_captures | 71 | 71 |
| relationships | 0 | 0 |
| size_bytes | 757760 | 757760 |
| superseded_facts | 90 | 90 |

## Ingestion decisions

- decisions: `{'memorized': 433, 'rejected': 71}`
- latency (ms): `{'count': 503, 'mean': 6583.2, 'median': 3531.0, 'p95': 17485.0, 'max': 65719.0}`

What ingestion deliberately ignored (sample):

- `cap_0016` -- transient_discard: The input is transient small talk/conversational filler with no durable signal.
- `cap_0036` -- transient_discard: Testing audio check/small talk with no durable signal
- `cap_0046` -- transient_discard: The user is in the middle of searching for a document; there is no durable signal here.
- `cap_0054` -- transient_discard: The input is a vague, non-durable remark about checking on an unspecified 'that' at an unspecified 'later' time; it contains no actionable task or meaningful entity information.
- `cap_0056` -- transient_discard: The user explicitly requested to disregard the previous input.
- `cap_0057` -- pii_detected: pre-LLM triage: password-like pattern detected ('password is/: <value>')
- `cap_0062` -- transient_discard: The input contains only filler phrases with no durable or actionable information.
- `cap_0065` -- transient_discard: User explicitly requested to disregard the capture.
- `cap_0070` -- transient_discard: The input is a vague, hypothetical musing ('Maybe I should...') rather than a grounded fact, event, or specific commitment.
- `cap_0075` -- transient_discard: The input is vague and contains no actionable or durable signal (it does not specify what 'that' refers to, nor does it provide a context for a commitment or task).

## Performance and usage

- agent_model_calls: `0`
- prompt_tokens: `None`
- completion_tokens: `None`
- total_tokens: `None`
- estimated_cost_usd: `not configured -- set KIVI_EVAL_COST_PER_1M_INPUT / _OUTPUT to price a run`
- retrieval_check_latency_ms_mean: `1.18`
- agent_latency_ms_mean: `3.3`
- agent_latency_ms_median: `2.9`
- agent_latency_ms_max: `6.7`
- ingestion_wall_clock_s: `None`
- db_growth_bytes: `0`
