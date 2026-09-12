# Kivi evaluation -- 2026-09-11T18:21:57+00:00

- mode: **offline (retrieval checks only)**
- database: `C:\Users\Rishabh Singh\Downloads\SVM_claude\evals\results\20260911_235157\eval.db`
- eval set: `C:\Users\Rishabh Singh\Downloads\SVM_claude\evals\eval_set.json` (12 cases)

## Results

- retrieval checks: **14/14 passed**

No failures.

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
- retrieval_check_latency_ms_mean: `1.46`
- agent_latency_ms_mean: `None`
- agent_latency_ms_median: `None`
- agent_latency_ms_max: `None`
- ingestion_wall_clock_s: `None`
- db_growth_bytes: `0`
