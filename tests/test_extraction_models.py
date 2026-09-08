"""
Hand-crafted tests for kivi/models/extraction.py -- a deliberate mix of valid
and invalid payloads. The invalid cases exist specifically to prove that
Pydantic rejects the kinds of malformed/dangerous data the design docs
called out: a commitment marked done without confirmation, a discarded
capture leaking content, a fact with no actual value, etc.

Run with:
    python -m pytest tests/test_extraction_models.py -v
"""

import pytest
from pydantic import ValidationError

from kivi.models.extraction import (
    Commitment,
    Event,
    ExtractionResult,
    Fact,
    Relationship,
)


# ---------------------------------------------------------------------------
# Valid cases
# ---------------------------------------------------------------------------

def test_valid_fact_with_numeric_value():
    fact = Fact(
        entity_mention="Meridian project",
        entity_type="project",
        attribute="budget",
        value_numeric=4500000,
        unit="INR",
        precision_class="exact_source",
        asserter_role="self",
    )
    assert fact.value_numeric == 4500000
    assert fact.value_text is None


def test_valid_commitment_open_not_confirmed():
    commitment = Commitment(
        commitment_mention="ask about budget",
        description="Ask the finance lead about the revised budget.",
        status="open",
        status_confirmed_by_user=False,
    )
    assert commitment.status == "open"
    assert commitment.status_confirmed_by_user is False


def test_valid_commitment_done_with_confirmation():
    commitment = Commitment(
        commitment_mention="ask about budget",
        description="Ask the finance lead about the revised budget.",
        status="done",
        status_confirmed_by_user=True,
    )
    assert commitment.status == "done"


def test_valid_commitment_blocked_with_reason():
    commitment = Commitment(
        commitment_mention="commit to timeline",
        description="Commit to a rollout timeline.",
        status="blocked",
        status_confirmed_by_user=False,
        blocking_reason="waiting on budget confirmation",
    )
    assert commitment.blocking_reason == "waiting on budget confirmation"


def test_valid_full_extraction_result():
    result = ExtractionResult(
        capture_id="cap_100",
        extraction_status="processed",
        facts=[
            Fact(
                entity_mention="Meridian project",
                entity_type="project",
                attribute="deadline",
                relative_time_expression="next Tuesday",
                resolved_time="2026-09-15T00:00:00",
                precision_class="exact_source",
                asserter_role="self",
                value_text="2026-09-15",
            )
        ],
        events=[
            Event(
                entity_mention="Simulink model",
                event_type="problem_encountered",
                description="model not converging due to sample time mismatch",
            )
        ],
        commitments=[
            Commitment(
                commitment_mention="ask about budget",
                description="Ask the finance lead about the revised budget.",
                status="open",
                status_confirmed_by_user=False,
            )
        ],
        relationships=[
            Relationship(
                source_mention="ask about budget",
                target_mention="commit to timeline",
                relationship_type="must_precede",
                reason="need the budget answer first",
            )
        ],
    )
    assert result.extraction_status == "processed"
    assert len(result.facts) == 1


def test_valid_discarded_capture_with_no_content():
    result = ExtractionResult(
        capture_id="cap_008",
        extraction_status="pii_detected",
        discard_reason="OTP detected in dictation; capture quarantined.",
    )
    assert result.facts == []
    assert result.discard_reason is not None


# ---------------------------------------------------------------------------
# Invalid cases
# ---------------------------------------------------------------------------

def test_invalid_fact_with_no_value_at_all():
    with pytest.raises(ValidationError, match="value_text or value_numeric"):
        Fact(
            entity_mention="Meridian project",
            entity_type="project",
            attribute="budget",
            precision_class="exact_source",
            asserter_role="self",
        )


def test_invalid_commitment_done_without_confirmation():
    """This is the specific rule the assignment called out: a commitment
    cannot be marked done unless the user explicitly confirmed it."""
    with pytest.raises(ValidationError, match="status_confirmed_by_user"):
        Commitment(
            commitment_mention="ask about budget",
            description="Ask the finance lead about the revised budget.",
            status="done",
            status_confirmed_by_user=False,
        )


def test_invalid_commitment_blocked_without_reason():
    with pytest.raises(ValidationError, match="blocking_reason"):
        Commitment(
            commitment_mention="commit to timeline",
            description="Commit to a rollout timeline.",
            status="blocked",
            status_confirmed_by_user=False,
        )


def test_invalid_extraction_result_missing_discard_reason():
    with pytest.raises(ValidationError, match="discard_reason"):
        ExtractionResult(
            capture_id="cap_009",
            extraction_status="transient_discard",
        )


def test_invalid_extraction_result_content_leak_on_discard():
    """A discarded capture must not smuggle facts through anyway -- this is
    the leakage guard, distinct from the missing-discard_reason case above."""
    with pytest.raises(ValidationError, match="must not carry any"):
        ExtractionResult(
            capture_id="cap_010",
            extraction_status="transient_discard",
            discard_reason="casual chatter, not worth storing",
            facts=[
                Fact(
                    entity_mention="weekend plans",
                    entity_type="general",
                    attribute="note",
                    value_text="thinking about a trip",
                    precision_class="spoken_approximation",
                    asserter_role="self",
                )
            ],
        )


def test_invalid_relationship_source_equals_target():
    with pytest.raises(ValidationError, match="must not be identical"):
        Relationship(
            source_mention="ask about budget",
            target_mention="ask about budget",
            relationship_type="must_precede",
        )
