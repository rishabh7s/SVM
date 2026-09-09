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
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from kivi.api import memory_ops
from kivi.api.session_store import SESSION_STORE, PendingClarification
from kivi.llm import LIGHT_MODEL, RETRIEVAL_MODEL, get_client
from kivi.retrieval import agent as agent_module
from kivi.retrieval.condensation import condense_query, enrich_with_clarification
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
