"""
The Kivi HTTP API: one chat/query endpoint implementing the session-aware
conversational engine (condensation -> agent -> clarification-on-miss), and
deterministic REST endpoints for direct memory management that never touch
an LLM.

Run with:
    uvicorn kivi.api.app:app --reload
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from kivi.api import memory_ops
from kivi.api.session_store import SESSION_STORE, PendingClarification
from kivi.ingestion.extractor import ExtractionFailed, extract_capture
from kivi.ingestion.triage import triage
from kivi.ingestion.vocab_log import VocabLogger
from kivi.ingestion.writer import apply_extraction_result, log_decision
from kivi.llm import LIGHT_MODEL, RETRIEVAL_MODEL, get_client
from kivi.retrieval import agent as agent_module
from kivi.retrieval.condensation import condense_query, condense_statement, enrich_with_clarification
from kivi.retrieval.tools import get_node_details

DB_PATH = Path(__file__).resolve().parents[2] / "db" / "kivi.db"

app = FastAPI(title="Kivi API")

# CORS: permissive by design for local frontend development against this
# backend -- allow_origins=["*"] is appropriate for a dev/demo API with no
# cookie-based auth (there's nothing here for a malicious origin to steal by
# virtue of CORS alone). Tighten to specific origins before any real
# deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Request/response contracts
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    question: str
    session_id: Optional[str] = None
    headless_mode: bool = True


class RichCitation(BaseModel):
    """The provenance contract: every citation is expanded with its full
    source detail, not just the bare (source_type, source_id) the agent
    itself produces -- built deterministically via get_node_details() after
    the agent returns, never asked of the LLM directly (safer and cheaper
    than trusting the model to carry transcript IDs/timestamps itself)."""

    source_type: str
    source_id: str
    snippet: str
    capture_id: Optional[str] = None
    captured_at: Optional[str] = None
    foreground_app: Optional[str] = None
    formatted_text: Optional[str] = None


class QueryMetrics(BaseModel):
    condensation_latency_ms: float
    retrieval_latency_ms: float  # time inside agent tool calls (search/details/edges/history/etc)
    generation_latency_ms: float  # time inside all model calls (condensation + agent loop)
    citation_enrichment_ms: float  # deterministic post-processing to build RichCitations
    total_latency_ms: float
    model_calls: int  # condensation call(s) + every agent loop turn
    tool_calls: int
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


class QueryResponse(BaseModel):
    session_id: str
    response_type: Literal["answer", "abstain", "needs_disambiguation", "needs_clarification"]
    response_text: Optional[str] = None
    abstain_reason: Optional[str] = None
    disambiguation_options: Optional[list[str]] = None
    clarification_prompt: Optional[str] = None
    citations: list[RichCitation] = Field(default_factory=list)
    condensed_query: Optional[str] = None  # what was actually sent to the agent, for transparency
    metrics: QueryMetrics


class MemoryUpdateRequest(BaseModel):
    updates: dict[str, Any]
    note: Optional[str] = None


class MemoryDeleteRequest(BaseModel):
    reason: Optional[str] = None


class IngestRequest(BaseModel):
    """One external transcript/note to ingest. Deliberately mirrors the
    shape kivi/ingestion/pipeline.py's batch CLI already consumes (a
    captures row) rather than inventing a parallel schema -- 'content' is
    what's stored as formatted_text, and this same record becomes
    immediately searchable through search_nodes once extraction runs,
    because it goes through the identical write path (triage -> extract ->
    kivi/ingestion/writer.py) that populates the unified_search FTS index
    for every other capture in this system.
    """

    content: str = Field(..., min_length=1, description="The raw transcript or note text.")
    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional. Recognized keys: capture_id (auto-generated if omitted), "
            "captured_at (ISO-8601; defaults to now), foreground_app, window_title."
        ),
    )
    source_type: Optional[Literal["speech", "selected_text"]] = Field(
        default="selected_text",
        description=(
            "Maps directly to captures.source_modality. 'manual_edit' is deliberately not a "
            "valid value here -- that modality is reserved for edits made through the memory "
            "management endpoints/tools (kivi/api/memory_ops.py), so provenance always tells "
            "the truth about whether a record came from external content or an in-app edit."
        ),
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional. When given and that session already has turns (from prior /query calls "
            "or prior /ingest calls in the same session), `content` is run through a "
            "declarative-preserving condensation pass (kivi.retrieval.condensation."
            "condense_statement) before extraction, so an implicit update like 'Update the "
            "budget to $400k' right after a conversation about Project Meridian resolves to "
            "'Update Project Meridian's budget to $400,000'. Omit for a fully self-contained "
            "note with no conversational context to resolve against."
        ),
    )


class IngestResponse(BaseModel):
    capture_id: str
    extraction_status: str
    discard_reason: Optional[str] = None
    facts_inserted: int = 0
    facts_superseded: int = 0
    facts_unchanged: int = 0
    events_inserted: int = 0
    commitments_created: int = 0
    commitment_status_changed: int = 0
    commitment_status_unchanged: int = 0
    preferences_inserted: int = 0
    preferences_superseded: int = 0
    preferences_unchanged: int = 0
    relationships_resolved: int = 0
    relationships_skipped: int = 0
    entity_resolutions: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    condensed_content: Optional[str] = Field(
        default=None,
        description="What was actually sent to extraction, if condensation ran and changed it "
        "(session_id given, session had prior turns, and used_history was true). None otherwise "
        "-- absence means 'content' was used exactly as given.",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Deterministic memory management -- direct UI actions. NEVER call an LLM.
# ---------------------------------------------------------------------------

@app.get("/memories/{memory_id}")
def get_memory(memory_id: str) -> dict:
    conn = get_db()
    try:
        result = memory_ops.get_memory(conn, memory_id)
    finally:
        conn.close()
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.patch("/memories/{memory_id}")
def update_memory(memory_id: str, body: MemoryUpdateRequest) -> dict:
    conn = get_db()
    try:
        result = memory_ops.update_memory(conn, memory_id, body.updates, note=body.note)
        if "error" in result:
            conn.rollback()
            raise HTTPException(status_code=400, detail=result["error"])
        conn.commit()
    finally:
        conn.close()
    return result


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: str, body: MemoryDeleteRequest = MemoryDeleteRequest()) -> dict:
    conn = get_db()
    try:
        result = memory_ops.delete_memory(conn, memory_id, reason=body.reason)
        if "error" in result:
            conn.rollback()
            status = 404 if "no " in result["error"] and "found" in result["error"] else 400
            raise HTTPException(status_code=status, detail=result["error"])
        conn.commit()
    finally:
        conn.close()
    return result


# ---------------------------------------------------------------------------
# Ingestion -- synchronous single-record entry point for external
# transcripts/notes. Deliberately reuses the SAME triage -> extract -> write
# path as the batch pipeline (kivi/ingestion/pipeline.py), rather than a
# parallel "just insert some rows" implementation -- that's what makes the
# result "immediately searchable via existing retrieval tools" true by
# construction: writer.py's INSERTs into declarative_facts/episodic_events/
# commitments fire the exact same unified_search triggers a batch-ingested
# capture would, so search_nodes finds this content with no separate
# indexing step.
# ---------------------------------------------------------------------------

@app.post("/ingest", response_model=IngestResponse)
def ingest(body: IngestRequest) -> IngestResponse:
    metadata = body.metadata or {}
    capture_id = metadata.get("capture_id") or f"cap_{uuid.uuid4().hex[:12]}"
    captured_at = metadata.get("captured_at") or datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    foreground_app = metadata.get("foreground_app")
    window_title = metadata.get("window_title")
    source_modality = body.source_type or "selected_text"

    # Flow 1 ("Put Info"): if a session_id is given AND that session already
    # has turns, resolve implicit references (e.g. "Update the budget to
    # $400k" right after a chat about Project Meridian) into a standalone
    # declarative statement BEFORE extraction -- using condense_statement,
    # never condense_query, so the rewrite can never drift into a question.
    # A session with no turns yet, or no session_id at all, ingests
    # body.content exactly as given (condense_statement's own no-history
    # short-circuit makes this a no-op call, but we skip the call entirely
    # when there's no session_id to avoid depending on GEMINI_API_KEY for
    # ingestion that doesn't need it).
    resolved_content = body.content
    condensed_content_for_response: Optional[str] = None
    if body.session_id:
        session = SESSION_STORE.get_or_create(body.session_id)
        if session.turns:
            condensation_client = get_client()
            condensation_result = condense_statement(condensation_client, LIGHT_MODEL, session, body.content)
            if condensation_result.used_history:
                resolved_content = condensation_result.standalone_query
                condensed_content_for_response = resolved_content
        # Record this ingestion as a turn too, so a /query in the same
        # session moments later can resolve references against it (e.g.
        # asking "what's its budget now?" right after this Put Info call).
        session.add_turn("user", resolved_content)
        SESSION_STORE.save(session)

    conn = get_db()
    try:
        existing = conn.execute("SELECT 1 FROM captures WHERE capture_id = ?", (capture_id,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"capture_id '{capture_id}' already exists")

        conn.execute(
            "INSERT INTO captures "
            "(capture_id, raw_asr_text, formatted_text, source_modality, foreground_app, window_title, "
            " captured_at, extraction_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (capture_id, body.content, resolved_content, source_modality, foreground_app, window_title, captured_at),
        )
        conn.commit()

        # --- Pre-LLM triage, identical to the batch pipeline: a flagged
        # secret is quarantined directly with NO extraction call, so it
        # never leaves the machine at all. ---
        triage_result = triage(body.content, resolved_content)
        if triage_result.flagged:
            discard_reason = f"pre-LLM triage: {triage_result.reason}"
            conn.execute(
                "UPDATE captures SET extraction_status = 'pii_detected', discard_reason = ? WHERE capture_id = ?",
                (discard_reason, capture_id),
            )
            log_decision(conn, capture_id, "rejected", reason=discard_reason)
            conn.commit()
            return IngestResponse(
                capture_id=capture_id, extraction_status="pii_detected", discard_reason=discard_reason,
                condensed_content=condensed_content_for_response,
            )

        client = get_client()  # raises RuntimeError -> FastAPI 500 if GEMINI_API_KEY is missing
        try:
            result = extract_capture(conn, client, capture_id, body.content, resolved_content, captured_at)
        except ExtractionFailed as e:
            conn.rollback()
            discard_reason = f"extraction failed after retries: {e.underlying}"
            conn.execute(
                "UPDATE captures SET extraction_status = 'incomplete_capture', discard_reason = ? WHERE capture_id = ?",
                (discard_reason, capture_id),
            )
            log_decision(conn, capture_id, "rejected", reason=discard_reason)
            conn.commit()
            return IngestResponse(
                capture_id=capture_id, extraction_status="incomplete_capture", discard_reason=discard_reason,
                condensed_content=condensed_content_for_response,
            )

        vocab_logger = VocabLogger()
        summary = apply_extraction_result(conn, capture_id, result, vocab_logger)

        conn.execute(
            "UPDATE captures SET extraction_status = ?, discard_reason = ? WHERE capture_id = ?",
            (result.extraction_status, result.discard_reason, capture_id),
        )
        log_decision(
            conn, capture_id, "memorized" if result.extraction_status == "processed" else "rejected",
            reason=None if result.extraction_status == "processed" else result.discard_reason,
            summary=summary,
        )
        conn.commit()

        return IngestResponse(
            capture_id=capture_id,
            extraction_status=result.extraction_status,
            discard_reason=result.discard_reason,
            facts_inserted=summary.facts_inserted,
            facts_superseded=summary.facts_superseded,
            facts_unchanged=summary.facts_unchanged,
            events_inserted=summary.events_inserted,
            commitments_created=summary.commitments_created,
            commitment_status_changed=summary.commitment_status_changed,
            commitment_status_unchanged=summary.commitment_status_unchanged,
            preferences_inserted=summary.preferences_inserted,
            preferences_superseded=summary.preferences_superseded,
            preferences_unchanged=summary.preferences_unchanged,
            relationships_resolved=summary.relationships_resolved,
            relationships_skipped=summary.relationships_skipped,
            entity_resolutions=[
                {"entity_id": r.entity_id, "method": r.method, "matched_alias": r.matched_alias, "similarity": r.similarity}
                for r in summary.entity_resolutions
            ],
            warnings=summary.warnings,
            condensed_content=condensed_content_for_response,
        )
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Session-aware conversational query
# ---------------------------------------------------------------------------

def _build_rich_citations(conn: sqlite3.Connection, citations: list) -> list[RichCitation]:
    rich = []
    for c in citations:
        details = get_node_details(conn, c.source_id)
        rich.append(
            RichCitation(
                source_type=c.source_type,
                source_id=c.source_id,
                snippet=c.snippet,
                capture_id=details.get("capture_id"),
                captured_at=details.get("captured_at"),
                foreground_app=details.get("foreground_app"),
                formatted_text=details.get("formatted_text"),
            )
        )
    return rich


NO_MATCH_PREFIX = "no_matching_nodes:"


@app.post("/query", response_model=QueryResponse)
def query(body: QueryRequest) -> QueryResponse:
    total_start = time.perf_counter()
    session = SESSION_STORE.get_or_create(body.session_id)

    client = get_client()  # raises a clear RuntimeError -> FastAPI 500 if GEMINI_API_KEY is missing
    condensation_latency_ms = 0.0
    condensation_model_calls = 0
    cond_result = None

    # --- Condensation / enrichment pre-step -------------------------------
    if session.pending_clarification is not None:
        t0 = time.perf_counter()
        cond_result = enrich_with_clarification(client, LIGHT_MODEL, session.pending_clarification, body.question)
        condensation_latency_ms += (time.perf_counter() - t0) * 1000
        condensation_model_calls += 1

        if not cond_result.used_history:
            # the reply still didn't resolve the gap -- ask again, refreshed
            pending = session.pending_clarification
            pending.missing_info_description = cond_result.reasoning or pending.missing_info_description
            session.pending_clarification = pending
            session.add_turn("user", body.question)
            clarification_prompt = f"I still don't have enough to find that -- {pending.missing_info_description}"
            session.add_turn("assistant", clarification_prompt)
            SESSION_STORE.save(session)
            return QueryResponse(
                session_id=session.session_id,
                response_type="needs_clarification",
                clarification_prompt=clarification_prompt,
                condensed_query=None,
                metrics=QueryMetrics(
                    condensation_latency_ms=condensation_latency_ms,
                    retrieval_latency_ms=0.0,
                    generation_latency_ms=condensation_latency_ms,
                    citation_enrichment_ms=0.0,
                    total_latency_ms=(time.perf_counter() - total_start) * 1000,
                    model_calls=condensation_model_calls,
                    tool_calls=0,
                ),
            )

        final_query = cond_result.standalone_query
        session.pending_clarification = None
    else:
        t0 = time.perf_counter()
        cond_result = condense_query(client, LIGHT_MODEL, session, body.question)
        condensation_latency_ms += (time.perf_counter() - t0) * 1000
        if session.turns:  # a model call only actually happened if there was history to condense against
            condensation_model_calls += 1
        final_query = cond_result.standalone_query

    session.add_turn("user", body.question)  # store what the user actually said, not the condensed rewrite

    # --- Agent run ----------------------------------------------------------
    conn = get_db()
    try:
        result = agent_module.run(conn, client, RETRIEVAL_MODEL, final_query, headless_mode=body.headless_mode)

        # --- Genuine retrieval miss -> active clarification, not a dead end ---
        if (
            not body.headless_mode
            and result.response.response_type == "abstain"
            and result.response.abstain_reason
            and result.response.abstain_reason.startswith(NO_MATCH_PREFIX)
        ):
            missing_desc = result.response.abstain_reason[len(NO_MATCH_PREFIX):].strip()
            session.pending_clarification = PendingClarification(
                original_question=body.question,
                condensed_question=final_query,
                missing_info_description=missing_desc,
            )
            clarification_prompt = missing_desc if missing_desc.endswith("?") else f"I couldn't find that -- {missing_desc}"
            session.add_turn("assistant", clarification_prompt)
            SESSION_STORE.save(session)
            return QueryResponse(
                session_id=session.session_id,
                response_type="needs_clarification",
                clarification_prompt=clarification_prompt,
                condensed_query=final_query,
                metrics=QueryMetrics(
                    condensation_latency_ms=condensation_latency_ms,
                    retrieval_latency_ms=result.metrics.retrieval_latency_ms,
                    generation_latency_ms=condensation_latency_ms + result.metrics.generation_latency_ms,
                    citation_enrichment_ms=0.0,
                    total_latency_ms=(time.perf_counter() - total_start) * 1000,
                    model_calls=condensation_model_calls + result.metrics.model_calls,
                    tool_calls=result.metrics.tool_calls,
                    prompt_tokens=result.metrics.prompt_tokens,
                    completion_tokens=result.metrics.completion_tokens,
                    total_tokens=result.metrics.total_tokens,
                ),
            )

        # --- Ordinary path: answer / abstain (non-retrieval-miss) / disambiguation ---
        t_cite = time.perf_counter()
        rich_citations = _build_rich_citations(conn, result.response.citations)
        citation_enrichment_ms = (time.perf_counter() - t_cite) * 1000
    finally:
        conn.close()

    response_text = result.response.derived_answer
    if result.response.response_type == "needs_disambiguation":
        options = result.response.disambiguation_options or []
        response_text = "Which did you mean? " + "; ".join(options)

    session.add_turn("assistant", response_text or result.response.abstain_reason or "(no response)")
    SESSION_STORE.save(session)

    return QueryResponse(
        session_id=session.session_id,
        response_type=result.response.response_type,
        response_text=response_text,
        abstain_reason=result.response.abstain_reason,
        disambiguation_options=result.response.disambiguation_options,
        citations=rich_citations,
        condensed_query=final_query if cond_result.used_history else None,
        metrics=QueryMetrics(
            condensation_latency_ms=condensation_latency_ms,
            retrieval_latency_ms=result.metrics.retrieval_latency_ms,
            generation_latency_ms=condensation_latency_ms + result.metrics.generation_latency_ms,
            citation_enrichment_ms=citation_enrichment_ms,
            total_latency_ms=(time.perf_counter() - total_start) * 1000,
            model_calls=condensation_model_calls + result.metrics.model_calls,
            tool_calls=result.metrics.tool_calls,
            prompt_tokens=result.metrics.prompt_tokens,
            completion_tokens=result.metrics.completion_tokens,
            total_tokens=result.metrics.total_tokens,
        ),
    )


# ---------------------------------------------------------------------------
# Session inspection (debugging/inspectability -- not strictly required by
# the contract, but cheap and consistent with this project's emphasis on
# "an engineer can inspect why memory did or did not affect a result")
# ---------------------------------------------------------------------------

@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    if not SESSION_STORE.exists(session_id):
        raise HTTPException(status_code=404, detail=f"no session found with id '{session_id}'")
    return SESSION_STORE.get_or_create(session_id).model_dump()


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    deleted = SESSION_STORE.delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"no session found with id '{session_id}'")
    return {"session_id": session_id, "deleted": True}
