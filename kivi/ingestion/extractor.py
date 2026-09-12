"""The extraction call: build the prompt, call the model, return a validated
ExtractionResult.

instructor's max_retries handles a response that doesn't fit the schema;
call_with_backoff handles the provider being unavailable. Different
failures, both wanted.

Not covered by the test suite -- it makes real network calls. writer.py is
what's unit-tested, using hand-built results.
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Optional

import instructor

from kivi.ingestion.entity_resolution import get_l1_context
from kivi.llm import DEFAULT_EXTRACTION_MODEL, get_extraction_client
from kivi.models.extraction import ExtractionResult
from kivi.retry import call_with_backoff

SYSTEM_PROMPT_TEMPLATE = """You extract structured memory from a single dictation or selected-text capture.

capture_id for this record: {capture_id}
This capture's own timestamp (use this as the anchor for resolving any relative time \
expression like "tomorrow" or "next Tuesday" to an absolute ISO-8601 resolved_time): {captured_at}

Known entities already on file (snap a mention to one of these entity_id/canonical_name \
pairs whenever it clearly refers to the same thing, rather than treating it as new):
{entity_context}

Categories -- put each piece of durable signal in exactly one:
- facts: a ground truth ABOUT something in the world (a budget, a role, a spec, a deadline).
- events: something that happened, anchored in time.
- commitments: something planned, promised, or owed.
- preferences: an enduring HABIT or operational constraint about HOW the user wants things \
done ("always summarize in bullet points", "David prefers async updates over meetings") -- \
never a one-off fact. Only set entity_mention/category on a preference when the text actually \
scopes it to a specific thing; a general standing instruction gets neither.

STRUCTURED SUMMARIES AND MEETING RECAPS -- read this whenever the capture looks like notes, \
minutes, a standup summary, a bulleted recap, or a status roll-up rather than one continuous \
spoken thought:
- Extract EVERY entity-attribute assertion in it, not just the first one or the headline. A \
recap listing four projects with a new owner each must produce four separate facts.
- A line that reassigns, hands over, promotes, or replaces someone IS a fact about the entity, \
not merely an event. "Driftwood now goes to Sofia", "Priya is taking over Helix", "Marcus is \
stepping off Meridian and Lena is picking it up" each assert a CURRENT value for that project's \
owner attribute. Emit the fact (the new value only -- never the person being replaced), and \
additionally emit an event ONLY if the handover itself is worth remembering as something that \
happened at a point in time.
- Use these EXACT attribute names whenever the assertion is one of these kinds, regardless of \
the wording in the text, so a later update lands on the same attribute and supersedes the \
earlier value instead of sitting beside it as a second "current" answer:
    owner       -- who owns / leads / is responsible for / is the DRI or point of contact for it
                   ("project lead", "running it", "taking over", "reporting owner" all map here)
    deadline    -- when it is due / ships / must land
    budget      -- how much money is allocated to it
    status      -- its current state (on track, blocked, paused, shipped, cancelled)
    start_date  -- when it begins / kicked off
- Record only the LATEST value asserted in this capture for a given entity+attribute. If a \
recap says "Lena had it, now it's Sofia", the fact's value is Sofia -- one fact, not two.
- Attribute names are lowercase snake_case. Never invent a near-duplicate of one of the names \
above ("project_owner", "owned_by", "lead" are all wrong -- use "owner").

Reject (set extraction_status to something other than 'processed', explain why in \
discard_reason, and leave facts/events/commitments/preferences/relationships ALL empty) when \
the content is:
- transient small talk with no durable signal ("feeling tired", "grabbing coffee", casual chatter);
- a counterfactual, hypothetical, or "what-if" musing ("Suppose the budget dropped...", \
"What if Sarah leaves?") -- these describe a possibility, not something true or decided, and \
must never be recorded as a fact or commitment;
- a secret/credential, or clearly truncated.

Never set a commitment's status to 'done' unless the user explicitly confirmed completion in \
this capture -- hedged language stays 'open' or 'in_progress'. A commitment with status \
'blocked' must include a blocking_reason. Only extract a relationship between two things this \
same capture actually mentions.

PROBLEMS AND THEIR FIXES -- this is the highest-value pattern in the whole extraction, because \
it is what lets the user avoid solving the same problem twice months later:
- When the capture describes something GOING WRONG (a bug, an outage, a failure, a deploy that \
broke, a job producing bad data), emit an event with event_type EXACTLY 'problem_encountered'.
- When it describes what FIXED it, what the ROOT CAUSE turned out to be, or a workaround that \
got things moving, emit a SEPARATE event with event_type EXACTLY 'resolution_found'. The \
description must carry the actual technical substance -- "cleared the CDN cache and pinned the \
build hash" is useful next time; "fixed the issue" is not.
- When the SAME capture contains both, ALWAYS emit a relationship linking them, with \
source_mention = the problem's event_type, target_mention = the resolution's event_type, and \
relationship_type EXACTLY 'resolves'. Do not skip this; the link is what makes the fix findable \
from the problem later.
- Use these exact two labels even when the wording differs ("it broke" / "turned out to be" / \
"sorted it by"), so that a search for past fixes finds them all.

ORDERING BETWEEN COMMITMENTS -- when the capture says one thing has to happen BEFORE another \
("I need to X before I can Y", "Y is waiting on X", "once X lands I can start Y", "can't do Y \
until X"), extract BOTH as commitments and emit a relationship with source_mention = the thing \
that must happen FIRST, target_mention = the thing that must wait, and relationship_type \
EXACTLY 'must_precede'. Getting the direction right matters: source is the prerequisite.

inferred_foreground_app: set this ONLY if the text itself explicitly names an application \
("In Slack...", "From Chrome...", "this Notion doc"). If no application is named in the text, \
leave it null -- never guess or infer one from context, tone, or subject matter.
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
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
) -> ExtractionResult:
    """Runs one extraction call."""
    l1_context = get_l1_context(conn)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        capture_id=capture_id,
        captured_at=captured_at,
        entity_context=_format_entity_context(l1_context),
    )

    def _call() -> ExtractionResult:
        return client.chat.completions.create(
            model=DEFAULT_EXTRACTION_MODEL,
            response_model=ExtractionResult,
            max_retries=2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": formatted_text or raw_asr_text},
            ],
        )

    try:
        return call_with_backoff(_call, on_retry=on_retry)
    except Exception as e:  # noqa: BLE001 -- instructor raises several exception types across providers
        raise ExtractionFailed(capture_id, e) from e
