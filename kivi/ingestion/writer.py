"""Writes an ExtractionResult to the database.

Split from the LLM call so supersession and entity resolution can be tested
with hand-built ExtractionResult objects and no network.

Facts and preferences supersede: deactivate the old row, insert the new one,
then point the old at the new. That order matters -- the partial unique
index is checked immediately, not at commit. Commitments keep a stable
identity row and version their status separately.

Relationships are only resolved between mentions in the same capture. A fix
described weeks after the problem it solves is never linked.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass, field

from kivi.ingestion.entity_resolution import ResolutionResult, get_l1_context, resolve_entity
from kivi.ingestion.vocab_log import VocabLogger
from kivi.models.extraction import Commitment, ExtractionResult, Fact, Preference


@dataclass
class WriteSummary:
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
    entity_resolutions: list[ResolutionResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _values_equal(a: Fact, existing_row: sqlite3.Row) -> bool:
    return (
        a.value_text == existing_row["value_text"]
        and a.value_numeric == existing_row["value_numeric"]
        and a.unit == existing_row["unit"]
    )


# Supersession keys on the literal attribute string, so "owner" and
# "project_owner" are two attributes and a project ends up with two live
# owners. Collapse only the ones where a duplicate gives a contradictory
# answer; everything else passes through normalised.
_ATTRIBUTE_SYNONYMS: dict[str, str] = {
    "owner": "owner",
    "owned_by": "owner",
    "project_owner": "owner",
    "project_lead": "owner",
    "lead": "owner",
    "leader": "owner",
    "responsible": "owner",
    "responsible_party": "owner",
    "dri": "owner",
    "point_of_contact": "owner",
    "poc": "owner",
    "assignee": "owner",
    "deadline": "deadline",
    "due_date": "deadline",
    "due": "deadline",
    "target_date": "deadline",
    "ship_date": "deadline",
    "delivery_date": "deadline",
    "budget": "budget",
    "budget_amount": "budget",
    "allocated_budget": "budget",
    "budget_ceiling": "budget",
    "status": "status",
    "current_status": "status",
    "project_status": "status",
    "state": "status",
    "start_date": "start_date",
    "kickoff_date": "start_date",
    "kickoff": "start_date",
}


def canonical_attribute(attribute: str) -> str:
    """Normalizes an extracted attribute name to the form used as the
    supersession key: lowercased, whitespace/hyphens collapsed to
    underscores, then mapped through _ATTRIBUTE_SYNONYMS if it's one of the
    attributes where a synonym would create a second 'current' value."""
    if not attribute:
        return attribute
    normalized = re.sub(r"[\s\-]+", "_", attribute.strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return _ATTRIBUTE_SYNONYMS.get(normalized, normalized)


def _find_existing_active_fact(
    conn: sqlite3.Connection, entity_id: str, attribute: str
) -> sqlite3.Row | None:
    """Finds the live fact this one should supersede."""
    exact = conn.execute(
        "SELECT * FROM declarative_facts "
        "WHERE entity_id = ? AND attribute = ? AND is_active = 1 AND deleted_at IS NULL",
        (entity_id, attribute),
    ).fetchone()
    if exact:
        return exact

    target = canonical_attribute(attribute)
    candidates = conn.execute(
        "SELECT * FROM declarative_facts "
        "WHERE entity_id = ? AND is_active = 1 AND deleted_at IS NULL "
        "ORDER BY created_at DESC, rowid DESC",
        (entity_id,),
    ).fetchall()
    for row in candidates:
        if canonical_attribute(row["attribute"]) == target:
            return row
    return None


def _write_fact(conn: sqlite3.Connection, fact: Fact, entity_id: str, capture_id: str, summary: WriteSummary) -> str:
    attribute = canonical_attribute(fact.attribute)
    existing = _find_existing_active_fact(conn, entity_id, attribute)

    if existing and _values_equal(fact, existing):
        summary.facts_unchanged += 1
        return existing["fact_id"]

    new_fact_id = f"fact_{uuid.uuid4().hex[:12]}"

    if existing:
        # deactivate first -- the partial unique index is checked immediately
        conn.execute(
            "UPDATE declarative_facts SET is_active = 0 WHERE fact_id = ?",
            (existing["fact_id"],),
        )

    conn.execute(
        """
        INSERT INTO declarative_facts
            (fact_id, entity_id, attribute, value_text, value_numeric, unit,
             precision_class, asserter_role, relative_time_expression, resolved_time,
             source_capture_id, is_active, superseded_by_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
        """,
        (
            # the CANONICAL attribute is what gets stored, so the next
            # update for this attribute finds and supersedes this row
            new_fact_id, entity_id, attribute, fact.value_text, fact.value_numeric, fact.unit,
            fact.precision_class, fact.asserter_role, fact.relative_time_expression, fact.resolved_time,
            capture_id,
        ),
    )

    if existing:
        # Now that the new row exists, point the old row's superseded_by_id
        # at it -- this FK can only be set after the referenced row exists.
        conn.execute(
            "UPDATE declarative_facts SET superseded_by_id = ? WHERE fact_id = ?",
            (new_fact_id, existing["fact_id"]),
        )
        summary.facts_superseded += 1
    else:
        summary.facts_inserted += 1

    return new_fact_id


def _status_equal(c: Commitment, existing_row: sqlite3.Row) -> bool:
    return (
        c.status == existing_row["status"]
        and c.status_confirmed_by_user == bool(existing_row["status_confirmed_by_user"])
        and c.blocking_reason == existing_row["blocking_reason"]
        and c.due_date_resolved == existing_row["due_date_resolved"]
    )


def _write_commitment(
    conn: sqlite3.Connection, commitment: Commitment, entity_id: str | None, capture_id: str, summary: WriteSummary
) -> str:
    existing_commitment = conn.execute(
        "SELECT commitment_id FROM commitments WHERE lower(commitment_mention) = lower(?) "
        "AND (entity_id IS ? OR entity_id = ?) "
        # skip deleted ones, or a later mention quietly resurrects a commitment
        # the user asked to forget
        "AND deleted_at IS NULL "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (commitment.commitment_mention, entity_id, entity_id),
    ).fetchone()

    if existing_commitment:
        commitment_id = existing_commitment["commitment_id"]
    else:
        commitment_id = f"com_{uuid.uuid4().hex[:12]}"
        conn.execute(
            "INSERT INTO commitments (commitment_id, commitment_mention, description, entity_id, source_capture_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (commitment_id, commitment.commitment_mention, commitment.description, entity_id, capture_id),
        )
        summary.commitments_created += 1

    existing_status = conn.execute(
        "SELECT * FROM commitment_status_events WHERE commitment_id = ? AND is_active = 1 "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (commitment_id,),
    ).fetchone()

    if existing_status and _status_equal(commitment, existing_status):
        summary.commitment_status_unchanged += 1
        return commitment_id

    # Don't let the extractor's default status reopen finished work. A later
    # capture that just mentions a done task arrives as status='open'. A real
    # reopening says in_progress or blocked, so only the default is refused.
    if (
        existing_status
        and existing_status["status"] == "done"
        and existing_status["status_confirmed_by_user"]
        and commitment.status == "open"
        and not commitment.status_confirmed_by_user
    ):
        summary.commitment_status_unchanged += 1
        summary.warnings.append(
            f"commitment '{commitment.commitment_mention}' kept at status='done' -- this capture "
            f"reported the default status='open' without an explicit reopening, which would have "
            f"silently reverted a user-confirmed completion"
        )
        return commitment_id

    new_status_id = f"cse_{uuid.uuid4().hex[:12]}"

    if existing_status:
        conn.execute(
            "UPDATE commitment_status_events SET is_active = 0 WHERE status_event_id = ?",
            (existing_status["status_event_id"],),
        )

    conn.execute(
        """
        INSERT INTO commitment_status_events
            (status_event_id, commitment_id, status, status_confirmed_by_user, blocking_reason,
             due_date_relative_expression, due_date_resolved, source_capture_id, is_active, superseded_by_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
        """,
        (
            new_status_id, commitment_id, commitment.status, int(commitment.status_confirmed_by_user),
            commitment.blocking_reason, commitment.due_date_relative_expression, commitment.due_date_resolved,
            capture_id,
        ),
    )

    if existing_status:
        conn.execute(
            "UPDATE commitment_status_events SET superseded_by_id = ? WHERE status_event_id = ?",
            (new_status_id, existing_status["status_event_id"]),
        )

    summary.commitment_status_changed += 1
    return commitment_id


def _normalized_preference_text(text: str) -> str:
    """Text key for detecting a restatement of the same preference: lowercased,
    punctuation dropped, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", "", (text or "").lower())).strip()


def _find_duplicate_preference(
    conn: sqlite3.Connection, entity_id: str | None, preference: Preference
) -> sqlite3.Row | None:
    """An existing ACTIVE preference with the same scope and the same
    normalized text, if one exists."""
    candidates = conn.execute(
        "SELECT * FROM preferences "
        "WHERE is_active = 1 AND deleted_at IS NULL "
        "  AND (entity_id IS ? OR entity_id = ?) "
        "  AND (category IS ? OR category = ?) "
        "ORDER BY created_at DESC, rowid DESC",
        (entity_id, entity_id, preference.category, preference.category),
    ).fetchall()
    target = _normalized_preference_text(preference.preference_text)
    if not target:
        return None
    for row in candidates:
        if _normalized_preference_text(row["preference_text"]) == target:
            return row
    return None


def _write_preference(
    conn: sqlite3.Connection,
    preference: Preference,
    entity_id: str | None,
    capture_id: str,
    summary: WriteSummary,
) -> str:
    """Mirrors _write_fact's supersession discipline, but ONLY when both
    entity_id and category are concrete -- matching db/schema.sql's partial
    unique index (idx_one_active_preference_per_entity_category), which is
    the only case the DB itself can enforce as a hard uniqueness constraint."""
    scoped = entity_id is not None and preference.category is not None

    if scoped:
        existing = conn.execute(
            "SELECT * FROM preferences WHERE entity_id = ? AND category = ? "
            "AND is_active = 1 AND deleted_at IS NULL "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (entity_id, preference.category),
        ).fetchone()
        if existing and existing["preference_text"] == preference.preference_text:
            summary.preferences_unchanged += 1
            return existing["preference_id"]
    else:
        existing = None

    # Unscoped preferences have no supersession key, so a restatement used to
    # append a new row every time -- four copies of the same focus-block rule.
    # Normalised text is a reliable enough key; a genuinely reworded
    # preference still differs and still appends.
    duplicate = _find_duplicate_preference(conn, entity_id, preference)
    if duplicate is not None:
        summary.preferences_unchanged += 1
        return duplicate["preference_id"]

    new_preference_id = f"pref_{uuid.uuid4().hex[:12]}"

    if existing:
        # deactivate first, same as _write_fact
        conn.execute("UPDATE preferences SET is_active = 0 WHERE preference_id = ?", (existing["preference_id"],))

    conn.execute(
        """
        INSERT INTO preferences
            (preference_id, entity_id, category, preference_text, is_active, superseded_by_id, source_capture_id)
        VALUES (?, ?, ?, ?, 1, NULL, ?)
        """,
        (new_preference_id, entity_id, preference.category, preference.preference_text, capture_id),
    )

    if existing:
        conn.execute(
            "UPDATE preferences SET superseded_by_id = ? WHERE preference_id = ?",
            (new_preference_id, existing["preference_id"]),
        )
        summary.preferences_superseded += 1
    else:
        summary.preferences_inserted += 1

    return new_preference_id


def log_decision(
    conn: sqlite3.Connection,
    capture_id: str,
    decision: str,
    reason: str | None = None,
    summary: "WriteSummary | None" = None,
    latency_ms: float | None = None,
) -> str:
    """Records one row in decision_logs for a capture that has finished
    processing, whether that ended in a pre-LLM triage rejection, a
    model-decided rejection (extraction_status != 'processed'), or a
    successful memorize."""
    if decision not in ("memorized", "rejected"):
        raise ValueError(f"decision must be 'memorized' or 'rejected', got {decision!r}")
    decision_log_id = f"dlog_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO decision_logs
            (decision_log_id, capture_id, decision, reason, facts_created, events_created,
             commitments_created, preferences_created, latency_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_log_id,
            capture_id,
            decision,
            reason,
            summary.facts_inserted if summary else 0,
            summary.events_inserted if summary else 0,
            summary.commitments_created if summary else 0,
            summary.preferences_inserted if summary else 0,
            latency_ms,
        ),
    )
    return decision_log_id


def apply_extraction_result(
    conn: sqlite3.Connection,
    capture_id: str,
    result: ExtractionResult,
    vocab_logger: VocabLogger | None = None,
) -> WriteSummary:
    """Writes one ExtractionResult into the database inside a single
    transaction."""

    summary = WriteSummary()

    if result.extraction_status != "processed":
        # defensive -- the Pydantic validator already guarantees this is empty
        return summary

    # entity_mention -> entity_id, resolved once per unique mention in this result
    l1_context = get_l1_context(conn)
    mention_to_entity: dict[str, str] = {}

    def resolve(mention: str | None, entity_type_hint: str) -> str | None:
        if mention is None:
            return None
        if mention not in mention_to_entity:
            resolution = resolve_entity(conn, mention, entity_type_hint, capture_id, l1_context)
            mention_to_entity[mention] = resolution.entity_id
            summary.entity_resolutions.append(resolution)
        return mention_to_entity[mention]

    # facts
    for fact in result.facts:
        entity_id = resolve(fact.entity_mention, fact.entity_type)
        _write_fact(conn, fact, entity_id, capture_id, summary)

    # events
    event_id_by_mention: dict[str, str] = {}
    for event in result.events:
        entity_id = resolve(event.entity_mention, "general") if event.entity_mention else None
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO episodic_events
                (event_id, entity_id, event_type, description, relative_time_expression,
                 resolved_time, asserter_role, source_capture_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, entity_id, event.event_type, event.description,
                event.relative_time_expression, event.resolved_time, event.asserter_role, capture_id,
            ),
        )
        summary.events_inserted += 1
        if vocab_logger:
            vocab_logger.observe_event_type(event.event_type)
        # index by event_type + description prefix so relationships can find it;
        # good enough within a single capture's small extraction result
        event_id_by_mention[event.event_type] = event_id

    # commitments
    commitment_id_by_mention: dict[str, str] = {}
    for commitment in result.commitments:
        entity_id = resolve(commitment.entity_mention, "general") if commitment.entity_mention else None
        commitment_id = _write_commitment(conn, commitment, entity_id, capture_id, summary)
        commitment_id_by_mention[commitment.commitment_mention] = commitment_id

    # scoped preferences supersede, unscoped ones append
    for preference in result.preferences:
        entity_id = resolve(preference.entity_mention, "general") if preference.entity_mention else None
        _write_preference(conn, preference, entity_id, capture_id, summary)

    # within-capture only, see module docstring
    all_mentions: dict[str, tuple[str, str]] = {}  # mention -> (source_type, id)
    for mention, cid in commitment_id_by_mention.items():
        all_mentions[mention] = ("commitment", cid)
    for mention, eid in event_id_by_mention.items():
        all_mentions[mention] = ("event", eid)

    for rel in result.relationships:
        source = all_mentions.get(rel.source_mention)
        target = all_mentions.get(rel.target_mention)
        if source is None or target is None:
            summary.relationships_skipped += 1
            summary.warnings.append(
                f"relationship '{rel.relationship_type}' skipped -- could not resolve "
                f"source={rel.source_mention!r} or target={rel.target_mention!r} within this capture"
            )
            continue

        relationship_id = f"rel_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO relationships
                (relationship_id, source_type, source_id, target_type, target_id,
                 relationship_type, reason, source_capture_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (relationship_id, source[0], source[1], target[0], target[1], rel.relationship_type, rel.reason, capture_id),
        )
        summary.relationships_resolved += 1
        if vocab_logger:
            vocab_logger.observe_relationship_type(rel.relationship_type)

    return summary
