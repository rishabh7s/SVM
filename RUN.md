# RUN.md

**Primary review method: local application.** (Update this line if that changes later in the build.)

The interface is a Streamlit app talking to a FastAPI backend over HTTP, reading a local SQLite database. Two processes, one database file, no deployment required.

---

## 1. Required runtimes and versions

- Python 3.10+ (developed against 3.12)
- `pip` for dependency installation
- `sqlite3` CLI is optional (only needed for manual DB inspection — `db/init_db.py` and `db/verify.py` use Python's stdlib `sqlite3` module and need no separate binary)

## 2. Required environment variables

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | **Yes**, for any ingestion/extraction/retrieval/API step that calls the LLM | Authenticates `kivi/llm.py` against Google's Gemini API via `instructor.from_genai(...)`. Get one at https://aistudio.google.com/apikey |
| `KIVI_EXTRACTION_MODEL` | No (optional) | Overrides the default model (`gemini-3.6-flash`) used for structured extraction during ingestion. Do not set this to `gemini-1.5-flash` or `gemini-2.0-flash` -- both are fully shut down. |
| `KIVI_LIGHT_MODEL` | No (optional) | Overrides the default model (`gemini-3.5-flash-lite`) used for query condensation -- the cheap, single-shot pre-step that rewrites conversational follow-ups into self-contained queries. |
| `KIVI_RETRIEVAL_MODEL` | No (optional) | Overrides the default model (`gemini-3.6-flash`) driving the graph-detective agent's full tool-calling loop. |

**For the evaluator:** copy `.env.example` to `.env` in the project root and set `GEMINI_API_KEY=<your key>` on the one line provided. `kivi/llm.py` calls `load_dotenv()` on import, so any script that imports it automatically picks up `.env` from the working directory — no manual `export` needed, though exporting `GEMINI_API_KEY` directly in the shell also works and takes precedence if both are set. Do not commit a real `.env` file; only `.env.example` (with the key left blank) belongs in the repository.

## 3. Exact commands to install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` is committed at the project root with pinned versions, covering the backend
(fastapi, uvicorn), the interface (streamlit, requests), the model client (google-genai,
instructor, pydantic, python-dotenv) and the test suite (pytest, httpx). No other installation
step is needed — the database is plain SQLite via the Python standard library.

## 4. Exact commands to create, migrate, and seed the database

**The repository ships with `db/kivi.db` already built and ingested** (the
509-record corpus from `kivi_corpus.json`). Nothing needs to be created to
review the product or run the evaluation — skip to section 5.

These commands are for rebuilding from scratch. Note that they produce a
database containing only the small seed narrative, **not** the corpus:

```bash
python3 db/init_db.py            # creates db/kivi.db from schema.sql + seed.sql
# or, to rebuild from a clean slate:
python3 db/init_db.py --reset
```

To get back to a corpus-loaded state after a reset, re-ingest (section 9).

**On migrations.** There is deliberately no incremental migration framework.
`db/schema.sql` is the single authoritative definition of the schema, applied
whole by `db/init_db.py`, and a schema change is applied by rebuilding and
re-ingesting rather than by a numbered diff. The reasoning: this is a
single-user local demo whose entire database is a regenerable 780 KB file
derived from a corpus that ships with the repository, so a rebuild is cheap
and always reproduces exactly the same state — whereas a migration chain
would be a second definition of the schema that can silently disagree with
the first. `python3 db/verify.py` runs 13 database-level checks and is the
gate that a rebuild produced a correct schema.

The tradeoff this accepts: there is no supported way to carry an existing
populated database across a schema change without re-ingesting it. If you
change `db/schema.sql`, rebuild and re-ingest.

## 5. Exact commands to start every required process

Two processes. Start the backend first, in its own terminal:

```bash
uvicorn kivi.api.app:app --reload --port 8000
```
Starts the Kivi API server at `http://127.0.0.1:8000`. Interactive OpenAPI docs are auto-served at `http://127.0.0.1:8000/docs`. CORS is enabled for all origins, so any local frontend dev server can call it directly.

Then the interface, in a second terminal:

```bash
streamlit run streamlit_app.py
```
Opens the Kivi app at `http://localhost:8501`. It talks to the backend over HTTP for everything that writes or touches a model, and reads `db/kivi.db` directly (read-only) for the Memory Inspector. If the backend is not running, the sidebar says so.

Batch ingestion (a one-off command, not a long-running process) is still run separately when needed:
```bash
python3 -m kivi.ingestion.pipeline --input corpus/sample_captures.json
```

## 6. The URL, application window, or interface to open

**`http://localhost:8501`** — the Kivi app. This is the primary interface and the one to review.

Five tabs, matching the product's own boundary between modes:

- **Hey Kivi** — the conversational agent. Semantic memory is fully active here: retrieval, citations, conversational deletion, and both write paths. Every answer expands to show the dictations it came from, with the app and timestamp.
- **Today** — outstanding work, ordered, with what you are *waiting on* kept separate from what you can act on. Reads `get_open_commitments` directly (deterministic, no model call), so the ordering Hey Kivi quotes can be seen raw. Scope it to one project with the selector.
- **Put info** — manual, session-aware ingestion. An implicit update typed right after a Hey Kivi conversation resolves against that conversation.
- **Dictation** — deliberately memory-inert. No network call, no session, no extraction. This tab exists to make the dictation / Hey Kivi boundary visible in the product itself.
- **Memory** — what Kivi currently knows, with provenance, and a toggle per view for superseded and deleted rows. Includes **Connections** (the `resolves` and `must_precede` edges) and the **Decision log**, which records why a capture did or did not become memory.
- **Corpus & reset** — import a corpus or reset the database, via the real CLI scripts.

`http://127.0.0.1:8000/docs` is also available for exercising the API directly (Swagger UI).

## 7. Primary interactions to try

In the **Hey Kivi** tab, with a corpus imported (see section 9):

| Ask | What it demonstrates |
|---|---|
| "Who currently owns Project Driftwood?" | Answers from the *current* fact; the superseded owner is filtered out of retrieval entirely. |
| "What's the budget for project Jinangxi?" | A numeric fact reads as `$40,000`, not `None`. |
| "What are my preferences for focus blocks and for meeting note formatting?" | A two-topic request is decomposed into one search per topic and both halves are answered. |
| "What did we decide about the Antarctic expedition permits?" | Abstains rather than inventing an answer from a loosely-matching record. |
| "Forget the budget for project Jinangxi." | Conversational deletion that is real: check the Memory Inspector afterwards, and the row is gone from the default view and marked deleted under the toggle. |
| "Remember that the Helix cutover moved to the 14th, Priya's covering it while Sam's out, and I owe the client a summary by Friday." | One sentence becomes two facts and a commitment, through the same pipeline a dictation takes. |
| "What was Project Driftwood's owner before the current one?" | History stays reachable when the question is explicitly about it. |
| "What should I work on first?" | Outstanding work ordered deterministically — overdue first, each entry explaining its own position — with blocked work separated out rather than presented as today's work. |
| "What am I currently blocked on, and who am I waiting on?" | The blocking reason in the user's own words, naming the vendor or person being waited on. |
| "The sync job is duplicating records again. How did we fix that last time?" | Solution reuse: finds the problem, follows its `resolves` edge, and returns the actual fix (the upsert key), not a vague recollection. |
| "What do I need to do before I can start the Trellis rewrite migration?" | Dependency ordering: the prerequisite belongs to a different entity, and is still surfaced. |

Every answer expands to show its citations, with the source capture, the app it came from, and when it was captured.

### Command-line checks

```bash
python3 db/verify.py                                    # DB-level verification loop (13 checks)
python -m pytest tests/ -v                               # full test suite: models, ingestion, retrieval, agent, memory ops, API
python3 -m kivi.ingestion.pipeline --input corpus/sample_captures.json   # run the batch ingestion pipeline
```

With the server running (`uvicorn kivi.api.app:app --reload`), example requests:
```bash
# Deterministic memory read (no LLM call)
curl http://127.0.0.1:8000/memories/fact_002

# Deterministic memory correction (no LLM call) -- creates a new version, never mutates in place
curl -X PATCH http://127.0.0.1:8000/memories/fact_002 \
  -H "Content-Type: application/json" \
  -d '{"updates": {"value_numeric": 4200000}, "note": "corrected via API"}'

# Deterministic soft-delete (no LLM call)
curl -X DELETE http://127.0.0.1:8000/memories/evt_001 -H "Content-Type: application/json" -d '{"reason": "duplicate"}'

# Conversational query (requires a real GEMINI_API_KEY)
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the Meridian budget?", "session_id": "demo", "headless_mode": false}'

# Follow-up in the same session -- exercises query condensation ("it" -> "the Meridian project")
curl -X POST http://127.0.0.1:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What was it before that?", "session_id": "demo", "headless_mode": false}'
```

Optional, requires a real `GEMINI_API_KEY` in `.env` (makes live API calls):
```bash
python -m kivi.llm                          # single-call extraction smoke test
```

**Note on test isolation:** `tests/test_ingestion_writer.py`, `tests/test_retrieval_tools.py`, `tests/test_agent_deterministic.py`, `tests/test_memory_ops.py`, and the deterministic half of `tests/test_api.py` test all the logic that doesn't require a live model call -- entity resolution, supersession, soft-deletion, tool dispatch, the false-premise checker, the headless-mode collapse, the full REST CRUD contract, CORS headers, and the `/query` metrics/provenance contract shape (which is verified even when the underlying model call fails, since a correctly-shaped response with honest timing data is itself part of the contract). None of these need `GEMINI_API_KEY` to be real or the network to be reachable. They run against disposable temp copies of `db/kivi.db`, never the real file.

Two tests in `test_api.py` (`test_live_followup_condensation_resolves_pronoun`, `test_live_clarification_flow_on_genuine_miss`) are marked `@pytest.mark.skipif` and only run automatically when `GEMINI_API_KEY` is set to something other than an obvious placeholder — these are the only tests in the suite that exercise the actual conversational condensation/clarification flow against a real model, and are the ones to watch when validating that behavior for real.

## 8. The exact command to run the candidate evaluation

```bash
python run_evaluation.py --offline     # no API key needed, no cost, runs in seconds
python run_evaluation.py                # full pipeline, requires GEMINI_API_KEY
```

**Run these against the database that ships with the repository.** `db/kivi.db`
is committed and already contains the ingested 509-record corpus
(`kivi_corpus.json`), so the evaluation reproduces the reported results
immediately. Do **not** run `db/init_db.py` first — that rebuilds the database
from `db/seed.sql`, which holds a small hand-written narrative for the test
suite, not the corpus these cases are written against. If you do, the
evaluation refuses to run and tells you so (exit code 2) rather than reporting
failures that describe the database instead of the system.

To rebuild the memory state from the corpus yourself instead (~500 extraction
calls, 40–60 minutes, needs `GEMINI_API_KEY`):

```bash
python run_evaluation.py --ingest kivi_corpus.json
```

Exit codes: `0` all cases passed · `1` one or more cases failed ·
`2` the database does not contain the corpus the eval set is written for.

`--offline` runs only the deterministic retrieval checks: it interrogates memory state through
the same tools the agent uses, with no model call, so the memory layer can be evaluated (and
regressions caught) without credentials. The full run additionally puts every case through the
real Hey Kivi loop and scores the answer, the citations, and — for mutation cases — the state of
the database afterwards.

Useful flags:

```bash
python run_evaluation.py --ingest kivi_corpus.json   # build a fresh DB from a corpus, then evaluate
python run_evaluation.py --case conversational_deletion_persists   # one case (repeatable)
python run_evaluation.py --eval-set path/to/your_cases.json         # a different eval set
```

Every run works on a **copy** of the database (written into the run's own results directory), so
evaluating never mutates the database you are inspecting — which matters because some cases
deliberately delete memories to prove deletion persists.

Cost: token counts are always reported. A dollar figure is only computed if you supply current
rates, so the report never invents a price:

```bash
export KIVI_EVAL_COST_PER_1M_INPUT=<your input rate>
export KIVI_EVAL_COST_PER_1M_OUTPUT=<your output rate>
```

Exit code is non-zero if any case fails, so it drops into CI unchanged.

## 9. The exact procedure for importing another corpus

```bash
python3 -m kivi.ingestion.pipeline --input path/to/any_corpus.json
```
or, equivalently, from the repository root:

```bash
python import_corpus.py path/to/any_corpus.json
```

The input file must be a JSON array of objects matching this schema:

| Field | Type | Required | Notes |
|---|---|---|---|
| `capture_id` | string | **yes** | Unique. Records whose `capture_id` already exists in the database are skipped, so re-running against the same file (or a file with overlapping records) is safe. |
| `captured_at` | string | **yes** | ISO-8601 (e.g. `2026-01-06T00:45:27Z`). Anchors relative-time resolution and determines processing order. |
| `source_modality` | string | no | One of `speech`, `selected_text`, `manual_edit` — enforced by a `CHECK` constraint in `db/schema.sql`; any other value is rejected at insert time. **Defaults to `speech`** when absent or null, so a corpus that doesn't carry this field imports without modification. |
| `raw_asr_text` | string | no | Defaults to `""`. |
| `formatted_text` | string | no | Defaults to `""`. |
| `foreground_app` | string \| null | no | Defaults to `null`. |
| `window_title` | string \| null | no | Defaults to `null`. |

A record missing `capture_id`, `captured_at`, or `source_modality` raises immediately and aborts the whole batch before any row is written — the loader sorts by `captured_at` up front, so this fails fast rather than partway through.

Example record:

```json
{
  "capture_id": "cap_0001",
  "captured_at": "2026-01-06T00:45:27Z",
  "source_modality": "speech",
  "raw_asr_text": "someone should probably look at the onboarding flow soon-ish",
  "formatted_text": "Someone should probably look at the onboarding flow soon-ish.",
  "foreground_app": "Slack",
  "window_title": "#marketing"
}
```

A corpus can also be imported from the **Corpus & reset** tab in the app, which runs the same
script as a subprocess.

To ingest a corpus into a throwaway database and evaluate against it in one step, without
touching `db/kivi.db`:

```bash
python run_evaluation.py --ingest path/to/any_corpus.json
```

`kivi_corpus.json` (509 records, one user) is the corpus this repository's own evaluation is
written against.

**A note on free-tier quota.** Gemini's free tier permits 15 requests per minute. A full
17-case agent evaluation exceeds that, so one or more cases may report **NOT EVALUATED** —
the harness reports a rate-limited case that way deliberately, rather than as a failure,
since a provider quota says nothing about the system. Re-run an individual case with
`python run_evaluation.py --case <id>` once quota resets, or use a paid key to get the whole
set in one run. `python run_evaluation.py --offline` makes no model calls and is never
affected.

## 10. Where evaluation results and memory state can be inspected

**Evaluation results** — each run writes a timestamped directory under `evals/results/`:

| file | contents |
|---|---|
| `results.json` | every case: the input, the memory retrieved/created/changed, its provenance, the resulting behaviour, the full tool trace, the decision reason, latency and tokens |
| `summary.md` | human-readable summary; failures are listed with why each one matters |
| `eval.db` | the exact database that run evaluated, kept for inspection |

`evals/results/latest.json` and `latest.md` always point at the most recent run.

**Memory state** — the Memory Inspector tab in the Streamlit app is the intended surface: it
browses entities, facts, events, commitments, preferences, relationships, the decision log, and
raw captures, with provenance on each. It shows only *current* memory by default; each tab has a
toggle to reveal superseded and deleted rows.

Or directly via SQLite.
```bash
sqlite3 db/kivi.db          # optional, only if the sqlite3 CLI is installed
.tables
SELECT * FROM declarative_facts WHERE is_active = 1;
SELECT * FROM commitment_status_events ORDER BY created_at;
SELECT capture_id, extraction_status, discard_reason FROM captures WHERE extraction_status != 'processed';
```
Vocabulary drift for the open `event_type`/`relationship_type` fields is written to `logs/vocab_observations.json` after each pipeline run. A Memory Inspector UI view will replace/supplement this in a later phase.

## 11. The exact procedure for resetting the system

```bash
python3 db/init_db.py --reset     # rebuild from schema.sql + seed.sql (keeps the demo seed data)
python3 reset_db.py                # wipe to an EMPTY database from schema.sql alone (no seed data)
```

`reset_db.py` uses only the standard library, takes no prompts, and exits non-zero on failure, so
it is safe to call from a review script. The **Corpus & reset** tab in the app runs it behind a
confirmation checkbox.

Evaluation runs never need a reset: each one works on its own copy of the database.

---

## Notes

- `.env.example` lists every environment variable the project currently uses. Keep it in sync as new variables are introduced.
- If the console prints `Both GOOGLE_API_KEY and GEMINI_API_KEY are set. Using GOOGLE_API_KEY.`, that is the Gemini SDK noticing two keys in your environment. It is harmless, but set only `GEMINI_API_KEY` to silence it.
- If `python -m pytest` reports import errors for the `kivi` package, confirm you're running it from the project root (the directory containing both `kivi/` and `tests/`) — `python -m pytest` adds the current working directory to `sys.path`, which is what makes `from kivi.models.extraction import ...` resolve.
