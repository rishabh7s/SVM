# RUN.md

**Primary review method: local application.** (Update this line if that changes later in the build.)

This file is being built incrementally as the project progresses. Sections below are complete through Phase 3 (Pydantic models + LLM client). Ingestion pipeline, retrieval agent, corpus, evaluation, and UI sections will be added as those phases land — sections not yet applicable are marked `PENDING`.

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

`requirements.txt` (create this at the project root if not already present):
```
pydantic>=2.0
instructor
google-genai
jsonref
python-dotenv
pytest
fastapi
uvicorn[standard]
httpx
```

## 4. Exact commands to create, migrate, and seed the database

```bash
python3 db/init_db.py            # creates db/kivi.db from schema.sql + seed.sql
# or, to rebuild from a clean slate:
python3 db/init_db.py --reset
```

## 5. Exact commands to start every required process

```bash
uvicorn kivi.api.app:app --reload --port 8000
```
Starts the Kivi API server at `http://127.0.0.1:8000`. Interactive OpenAPI docs are auto-served at `http://127.0.0.1:8000/docs`. CORS is enabled for all origins, so any local frontend dev server can call it directly.

Batch ingestion (a one-off command, not a long-running process) is still run separately when needed:
```bash
python3 -m kivi.ingestion.pipeline --input corpus/sample_captures.json
```

## 6. The URL, application window, or interface to open

`http://127.0.0.1:8000/docs` — interactive API documentation (Swagger UI), where every endpoint below can be tried directly in the browser. There is no separate frontend in this repository; the API is the deliverable at this phase.

## 7. Primary interactions to try

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
python -m kivi.retrieval.manual_queries     # 7 hand-picked interactive-mode queries against the seeded data
```

**Note on test isolation:** `tests/test_ingestion_writer.py`, `tests/test_retrieval_tools.py`, `tests/test_agent_deterministic.py`, `tests/test_memory_ops.py`, and the deterministic half of `tests/test_api.py` test all the logic that doesn't require a live model call -- entity resolution, supersession, soft-deletion, tool dispatch, the false-premise checker, the headless-mode collapse, the full REST CRUD contract, CORS headers, and the `/query` metrics/provenance contract shape (which is verified even when the underlying model call fails, since a correctly-shaped response with honest timing data is itself part of the contract). None of these need `GEMINI_API_KEY` to be real or the network to be reachable. They run against disposable temp copies of `db/kivi.db`, never the real file.

Two tests in `test_api.py` (`test_live_followup_condensation_resolves_pronoun`, `test_live_clarification_flow_on_genuine_miss`) are marked `@pytest.mark.skipif` and only run automatically when `GEMINI_API_KEY` is set to something other than an obvious placeholder — these are the only tests in the suite that exercise the actual conversational condensation/clarification flow against a real model, and are the ones to watch when validating that behavior for real.

## 8. The exact command to run the candidate evaluation

`PENDING` — evaluation harness (`eval/run_eval.py`) not yet built.

## 9. The exact procedure for importing another corpus

```bash
python3 -m kivi.ingestion.pipeline --input path/to/any_corpus.json
```
The input file must be a JSON array of objects with at minimum: `capture_id`, `source_modality` (`speech` or `selected_text`), `captured_at` (ISO-8601), and `raw_asr_text` and/or `formatted_text`. `foreground_app` and `window_title` are optional. Records whose `capture_id` already exists in the database are skipped, so re-running against the same file (or a file with overlapping records) is safe.

## 10. Where evaluation results and memory state can be inspected

Currently: directly via the SQLite database.
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
python3 db/init_db.py --reset
```

---

## Notes

- `.env.example` lists every environment variable the project currently uses. Keep it in sync as new variables are introduced in later phases.
- If `python -m pytest` reports import errors for the `kivi` package, confirm you're running it from the project root (the directory containing both `kivi/` and `tests/`) — `python -m pytest` adds the current working directory to `sys.path`, which is what makes `from kivi.models.extraction import ...` resolve.
