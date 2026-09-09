"""
Query Condensation / Context Synthesis: a single lightweight model call
(LIGHT_MODEL, see kivi/llm.py) that rewrites an ambiguous follow-up
question into a fully self-contained standalone query BEFORE it ever
reaches the graph-detective agent or any database tool.

This runs entirely outside the agent's tool-calling loop -- it never
touches search_nodes, get_node_details, or any other tool, and it never
sees the database. Its only inputs are the rolling conversation history
and the new question; its only output is a rewritten question (or the
original, unchanged, when no rewriting was needed). This keeps
conversational context handling completely separate from the retrieval
tools themselves, per the requirement that session awareness must not leak
into the core retrieval layer -- kivi/retrieval/tools.py and
kivi/retrieval/agent.py have no knowledge that sessions or condensation
exist at all.

Two deliberate cost/latency optimizations, both checked before making any
model call:
  1. If the session has no prior turns, the question is condensed by
     definition (there's nothing to resolve a reference against) --
     skip the model call entirely and return the question as-is.
  2. If the session has a pending_clarification, this module is called in
     "enrichment" mode instead of "condensation" mode -- a different, more
     targeted prompt that merges the original question with the user's
     clarifying reply, rather than treating the reply as a fresh question
     to condense against general history.
"""

from __future__ import annotations

from typing import Optional

import instructor
from pydantic import BaseModel, Field

from kivi.api.session_store import PendingClarification, SessionState

CONDENSE_SYSTEM_PROMPT = """You rewrite a user's follow-up question into a fully self-contained \
standalone query, using the recent conversation history to resolve anaphora and implicit \
references (e.g. "it", "that", "the project", "him") into the actual thing being referred to.

Rules:
- If the new question is ALREADY fully self-contained (names its own subject clearly, no \
  pronoun or implicit reference that depends on prior turns), return it completely unchanged \
  and set used_history=false.
- If it depends on prior turns to make sense, rewrite it into a standalone version that names \
  the actual entity/subject explicitly, and set used_history=true.
- NEVER answer the question yourself. NEVER add information that wasn't in the history or the \
  question. You are rewriting, not answering.
- If the history doesn't actually contain enough to resolve a reference (e.g. "it" could mean \
  several different things mentioned, or nothing was mentioned that "it" could refer to), leave \
  the ambiguous reference as-is rather than guessing which one -- set used_history=false and \
  explain why in reasoning.
"""

ENRICH_SYSTEM_PROMPT = """The user was previously asked a question that couldn't be answered \
because something was missing (an entity name, a date, an app, etc). They have now replied with \
the missing information. Merge their original question with this new information into ONE fully \
self-contained standalone query.

Rules:
- Combine the original question's intent with the new clarifying detail -- don't just concatenate \
  them, actually rewrite into one coherent question.
- NEVER answer the question yourself, only rewrite it.
- If the reply still doesn't actually provide what was missing, set used_history=false and explain \
  why in reasoning -- the caller will fall back to asking again rather than guessing.
"""


class CondensationResult(BaseModel):
    standalone_query: str = Field(..., description="The fully self-contained, rewritten query.")
    used_history: bool = Field(..., description="True if prior context was actually needed/used to resolve the query.")
    reasoning: str = Field(default="", description="Brief note on what was resolved, or why nothing needed resolving.")


def _format_history(session: SessionState) -> str:
    lines = []
    for turn in session.recent_history():
        lines.append(f"{turn.role}: {turn.content}")
    return "\n".join(lines) if lines else "(no prior turns)"


def condense_query(
    client: instructor.Instructor,
    model: str,
    session: SessionState,
    new_question: str,
) -> CondensationResult:
    """Standard condensation path: rewrite new_question using session
    history. Skips the model call entirely for a session's first turn."""
    if not session.turns:
        return CondensationResult(
            standalone_query=new_question,
            used_history=False,
            reasoning="first turn in session -- nothing to condense against",
        )

    history_text = _format_history(session)
    return client.chat.completions.create(
        model=model,
        response_model=CondensationResult,
        max_retries=2,
        messages=[
            {"role": "system", "content": CONDENSE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Recent conversation:\n{history_text}\n\nNew question: {new_question}",
            },
        ],
    )


def enrich_with_clarification(
    client: instructor.Instructor,
    model: str,
    pending: PendingClarification,
    clarifying_reply: str,
) -> CondensationResult:
    """Clarification-response path: merge the original (condensed) question
    with the user's reply to the system's own clarifying question. This is
    a DIFFERENT prompt from condense_query's, not a reuse of it -- the task
    here is "merge a known gap with its answer," not "resolve a pronoun
    against general history," and conflating the two prompts would make
    both worse at their actual job."""
    return client.chat.completions.create(
        model=model,
        response_model=CondensationResult,
        max_retries=2,
        messages=[
            {"role": "system", "content": ENRICH_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Original question: {pending.condensed_question}\n"
                    f"What was missing: {pending.missing_info_description}\n"
                    f"User's clarifying reply: {clarifying_reply}"
                ),
            },
        ],
    )
