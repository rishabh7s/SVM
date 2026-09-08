"""
Writes a validated ExtractionResult into the database. This is factored out
from the LLM call itself (see extractor.py) specifically so it can be
tested with hand-crafted ExtractionResult objects, with no network call and
no dependency on what the LLM actually returns -- the correctness of
supersession/entity-resolution logic and the correctness of the LLM's
extraction are two separate concerns and should be testable separately.

Supersession discipline:
  - declarative_facts: identical to the pattern already enforced by the DB's
    partial unique index -- a new active fact for an existing
    (entity_id, attribute) always deactivates the old one in the same
    transaction; an identical value is a no-op (skipped) to avoid churn.
  - commitments: the exact same pattern, one level removed -- the
    commitments row (identity) is created once and never touched again;
    each status change is a new commitment_status_events row that
    deactivates the previous active one.

Relationship resolution is intentionally scoped to mentions produced within
the SAME capture's extraction result. Resolving a relationship whose source
or target was defined in an earlier capture would require applying the same
kind of fuzzy mention-resolution used for entities to facts/events/
commitments too -- a real capability, but out of scope for this narrower
first version. Relationships that can't be resolved within-capture are
logged and skipped, not silently dropped without a trace.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field

from kivi.ingestion.entity_resolution import ResolutionResult, get_l1_context, resolve_entity
from kivi.ingestion.vocab_log import VocabLogger
from kivi.models.extraction import Commitment, ExtractionResult, Fact


@dataclass
class WriteSummary:
    facts_inserted: int = 0
    facts_superseded: int = 0
    facts_unchanged: int = 0
    events_inserted: int = 0
    commitments_created: int = 0
    commitment_status_changed: int = 0
    commitment_status_unchanged: int = 0
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


def _write_fact(conn: sqlite3.Connection, fact: Fact, entity_id: str, capture_id: str, summary: WriteSummary) -> str:
    existing = conn.execute(
        "SELECT * FROM declarative_facts WHERE entity_id = ? AND attribute = ? AND is_active = 1",
        (entity_id, fact.attribute),
    ).fetchone()

    if existing and _values_equal(fact, existing):
        summary.facts_unchanged += 1
        return existing["fact_id"]

    new_fact_id = f"fact_{uuid.uuid4().hex[:12]}"

    if existing:
        # Deactivate the old row FIRST -- the partial unique index
        # (one active fact per entity_id/attribute) is checked immediately,
        # not deferred to transaction end, so the new active row cannot be
        # inserted while the old one is still active=1.
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
            new_fact_id, entity_id, fact.attribute, fact.value_text, fact.value_numeric, fact.unit,
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
        "AND (entity_id IS ? OR entity_id = ?)",
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
        "SELECT * FROM commitment_status_events WHERE commitment_id = ? AND is_active = 1",
        (commitment_id,),
    ).fetchone()

    if existing_status and _status_equal(commitment, existing_status):
        summary.commitment_status_unchanged += 1
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


def apply_extraction_result(
    conn: sqlite3.Connection,
    capture_id: str,
    result: ExtractionResult,
    vocab_logger: VocabLogger | None = None,
) -> WriteSummary:
    """Writes one ExtractionResult into the database inside a single
    transaction. Caller is responsible for conn.commit() / conn.rollback()
    around this call -- kept out of this function so a batch pipeline can
    control transaction boundaries per-capture."""

    summary = WriteSummary()

    if result.extraction_status != "processed":
        # Nothing to write for a quarantined/discarded capture -- the
        # Pydantic model itself already guarantees facts/events/commitments
        # are empty in this case (see extraction.py's leakage-guard
        # validator), so this is a defensive no-op, not the primary guard.
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

    # relationships -- resolved only against mentions produced by this same
    # capture (see module docstring for why cross-capture resolution is
    # out of scope for now)
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
