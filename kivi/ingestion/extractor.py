"""
Wraps the actual LLM call: builds the extraction prompt (including the L1
entity context and the capture's own captured_at as the temporal anchor),
calls the model via instructor, and returns a validated ExtractionResult.

instructor's own max_retries handles "retry with the validation error fed
back to the model" internally when response_model is set -- so the "single
retry-with-error-message on validation failure" requirement is satisfied by
instructor's built-in mechanism (max_retries=2 = one real attempt + one
retry), not a hand-rolled retry loop. If both attempts fail, this raises
ExtractionFailed, which pipeline.py catches and turns into an
incomplete_capture record.

This module makes real network calls and is therefore NOT covered by the
automated test suite -- kivi/ingestion/writer.py is what's unit-tested,
using hand-crafted ExtractionResult objects as a stand-in for what this
module would return.
"""

from __future__ import annotations

import sqlite3

import instructor

from kivi.ingestion.entity_resolution import get_l1_context
from kivi.llm import DEFAULT_EXTRACTION_MODEL, get_extraction_client
from kivi.models.extraction import ExtractionResult

SYSTEM_PROMPT_TEMPLATE = """You extract structured memory from a single dictation or selected-text capture.

capture_id for this record: {capture_id}
This capture's own timestamp (use this as the anchor for resolving any relative time \
expression like "tomorrow" or "next Tuesday" to an absolute ISO-8601 resolved_time): {captured_at}

Known entities already on file (snap a mention to one of these entity_id/canonical_name \
pairs whenever it clearly refers to the same thing, rather than treating it as new):
{entity_context}

Rules:
- Set extraction_status to something other than 'processed' (and explain why in \
discard_reason) if the content is casual chatter, a secret/credential, or clearly \
truncated. In that case, facts/events/commitments/relationships must all be empty.
- Never set a commitment's status to 'done' unless the user explicitly confirmed \
completion in this capture -- hedged language stays 'open' or 'in_progress'.
- A commitment with status 'blocked' must include a blocking_reason.
- Only extract a relationship between two things this same capture actually mentions.
"""


class ExtractionFailed(Exception):
    def __init__(self, capture_id: str, underlying: Exception):
        self.capture_id = capture_id
        self.underlying = underlying
        super().__init__(f"Extraction failed for {capture_id} after retries: {underlying}")


def _format_entity_context(l1_context: list[dict]) -> str:
    if not l1_context:
        return "(none yet -- this may be one of the first captures processed)"
    lines = []
    for entity in l1_context[:50]:
        aliases = ", ".join(entity["aliases"][:5])
        lines.append(f"- {entity['entity_id']} ({entity['entity_type']}): {entity['canonical_name']} [aliases: {aliases}]")
    return "\n".join(lines)


def extract_capture(
    conn: sqlite3.Connection,
    client: instructor.Instructor,
    capture_id: str,
    raw_asr_text: str,
    formatted_text: str,
    captured_at: str,
) -> ExtractionResult:
    l1_context = get_l1_context(conn)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        capture_id=capture_id,
        captured_at=captured_at,
        entity_context=_format_entity_context(l1_context),
    )

    try:
        return client.chat.completions.create(
            model=DEFAULT_EXTRACTION_MODEL,
            response_model=ExtractionResult,
            max_retries=2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": formatted_text or raw_asr_text},
            ],
        )
    except Exception as e:  # noqa: BLE001 -- instructor raises several exception types across providers
        raise ExtractionFailed(capture_id, e) from e
