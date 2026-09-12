# Kivi evaluation -- 2026-09-12T14:12:39+00:00

- mode: **offline (retrieval checks only)**
- database: `C:\Users\Rishabh Singh\Downloads\SVM_claude\evals\results\20260912_194239\eval.db`
- eval set: `C:\Users\Rishabh Singh\Downloads\SVM_claude\evals\eval_set.json` (17 cases)

## Results

- retrieval checks: **18/18 passed**

No failures.

## Database

| metric | before | after |
|---|---:|---:|
| active_facts | 198 | 198 |
| captures | 516 | 516 |
| commitment_status_events | 163 | 163 |
| commitments | 122 | 122 |
| decision_logs | 536 | 536 |
| declarative_facts | 298 | 298 |
| entities | 51 | 51 |
| episodic_events | 162 | 162 |
| preferences | 39 | 39 |
| rejected_captures | 53 | 53 |
| relationships | 9 | 9 |
| size_bytes | 798720 | 798720 |
| superseded_facts | 100 | 100 |

## Ingestion decisions

- decisions: `{'memorized': 461, 'rejected': 75}`
- latency (ms): `{'count': 535, 'mean': 6439.0, 'median': 3531.0, 'p95': 16875.0, 'max': 65719.0}`

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
- retrieval_check_latency_ms_mean: `0.89`
- agent_latency_ms_mean: `None`
- agent_latency_ms_median: `None`
- agent_latency_ms_max: `None`
- ingestion_wall_clock_s: `None`
- db_growth_bytes: `0`
