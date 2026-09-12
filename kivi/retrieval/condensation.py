"""Rewrites a follow-up into a standalone query before it reaches the agent.

Never touches the database or any tool -- its only inputs are the recent
turns and the new message. Skips the model call entirely on a session's
first turn, since there is nothing to resolve against.

Three prompts, not one: condense_query for questions, condense_statement for
updates (so an update never drifts into a question), and
enrich_with_clarification for merging a reply with the gap it fills.
"""

from __future__ import annotations

from typing import Optional

import instructor
from pydantic import BaseModel, Field

from kivi.api.session_store import PendingClarification, SessionState
from kivi.retry import call_with_backoff

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
    """Standard condensation path: rewrite new_question using session history."""
    if not session.turns:
        return CondensationResult(
            standalone_query=new_question,
            used_history=False,
            reasoning="first turn in session -- nothing to condense against",
        )

    history_text = _format_history(session)
    # retry -- a throttled condensation kills the turn before retrieval starts
    return call_with_backoff(lambda: client.chat.completions.create(
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
    ))


CONDENSE_STATEMENT_SYSTEM_PROMPT = """You rewrite a user's manual update/note into a fully self-contained \
standalone DECLARATIVE STATEMENT, using the recent conversation history to resolve anaphora and \
implicit references (e.g. "it", "that", "the project", "the budget") into the actual thing being \
referred to.

This is the SAME kind of reference resolution as ordinary query condensation, but the input is a \
statement (an update, a correction, a note), not a question -- and the output must ALSO stay a \
statement. Never rephrase a declarative update into a question, and never answer anything.

Rules:
- If the new text is ALREADY fully self-contained (names its own subject clearly), return it \
  completely unchanged and set used_history=false.
- If it depends on prior turns to make sense (e.g. "Update the budget to $400k" after a \
  conversation about Project Meridian), rewrite it into a standalone declarative statement that \
  names the actual entity/subject explicitly (e.g. "Update Project Meridian's budget to $400,000"), \
  and set used_history=true.
- Preserve the statement's own grammatical mood exactly -- an imperative stays an imperative, a \
  plain assertion stays a plain assertion. Do NOT turn it into a question under any circumstance.
- If the history doesn't actually contain enough to resolve a reference, leave the ambiguous \
  reference as-is rather than guessing -- set used_history=false and explain why in reasoning.
"""


def condense_statement(
    client: instructor.Instructor,
    model: str,
    session: SessionState,
    new_statement: str,
) -> CondensationResult:
    """Declarative-preserving counterpart to condense_query, used by the manual
    ingestion path (POST /ingest's Flow 1 / "Put Info") instead of
    condense_query itself."""
    if not session.turns:
        return CondensationResult(
            standalone_query=new_statement,
            used_history=False,
            reasoning="first turn in session -- nothing to condense against",
        )

    history_text = _format_history(session)
    return call_with_backoff(lambda: client.chat.completions.create(
        model=model,
        response_model=CondensationResult,
        max_retries=2,
        messages=[
            {"role": "system", "content": CONDENSE_STATEMENT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Recent conversation:\n{history_text}\n\nNew update/note: {new_statement}",
            },
        ],
    ))


def enrich_with_clarification(
    client: instructor.Instructor,
    model: str,
    pending: PendingClarification,
    clarifying_reply: str,
) -> CondensationResult:
    """Clarification-response path: merge the original (condensed) question
    with the user's reply to the system's own clarifying question."""
    return call_with_backoff(lambda: client.chat.completions.create(
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
    ))
