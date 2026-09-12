# Kivi evaluation -- 2026-09-11T22:46:13+00:00

- mode: **full pipeline (agent model: gemini-3.1-flash-lite)**
- database: `C:\Users\Rishabh Singh\Downloads\SVM_claude\evals\results\20260912_041613\eval.db`
- eval set: `C:\Users\Rishabh Singh\Downloads\SVM_claude\evals\eval_set.json` (17 cases)

## Results

- retrieval checks: **18/18 passed**
- agent cases: **15/17 passed**
- post-run state checks: **1/1 passed**

## Failures (kept visible on purpose)

### what_am_i_waiting_on -- What am I currently blocked on, and who am I waiting on?
- why it matters: Blocked commitments carry the user's own blocking_reason. That text IS the answer to 'what am I waiting on' -- the agent must surface it rather than paraphrase it away or list blocked items as actionable work.
- agent: expected response_type='answer', got 'abstain'
- agent: answer does not mention 'vendor'
- agent: answer carries no citation

### solution_reuse_finds_past_fix -- The sync job is duplicating records again. How did we fix that last time?
- why it matters: The product's central promise is that the user does not solve the same problem twice. This is a two-hop answer -- find the problem, then follow its 'resolves' edge to the fix -- and the fix has to come back with its actual technical substance (the upsert key), not a vague 'we fixed it'. Answering with the problem alone, or with a fix the graph does not link, is the failure this guards.
- agent: expected response_type='answer', got 'abstain'
- agent: answer does not mention 'external id'
- agent: answer carries no citation
- agent: get_connected_edges was never called (called: none)

## Database

| metric | before | after |
|---|---:|---:|
| active_facts | 198 | 197 |
| captures | 515 | 515 |
| commitment_status_events | 163 | 163 |
| commitments | 122 | 122 |
| decision_logs | 535 | 535 |
| declarative_facts | 298 | 298 |
| entities | 51 | 51 |
| episodic_events | 161 | 161 |
| preferences | 39 | 39 |
| rejected_captures | 53 | 53 |
| relationships | 9 | 9 |
| size_bytes | 798720 | 798720 |
| superseded_facts | 100 | 101 |

## Ingestion decisions

- decisions: `{'memorized': 460, 'rejected': 75}`
- latency (ms): `{'count': 534, 'mean': 6444.3, 'median': 3523.5, 'p95': 16875.0, 'max': 65719.0}`

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

- agent_model_calls: `46`
- prompt_tokens: `283929`
- completion_tokens: `6725`
- total_tokens: `290654`
- estimated_cost_usd: `not configured -- set KIVI_EVAL_COST_PER_1M_INPUT / _OUTPUT to price a run`
- retrieval_check_latency_ms_mean: `2.87`
- agent_latency_ms_mean: `9354.4`
- agent_latency_ms_median: `5166.9`
- agent_latency_ms_max: `30796.4`
- ingestion_wall_clock_s: `None`
- db_growth_bytes: `0`
