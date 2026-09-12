"""The extraction contract.

Constraints the JSON Schema expressed as if/then are Pydantic validators
here, so a violation is caught when instructor parses the response, before
it can reach the database. The important ones: a fact needs a value, a
'blocked' commitment needs a reason, 'done' needs explicit confirmation, and
a discarded capture must carry no content at all.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Shared literals (mirrors the JSON Schema's enum fields)
# ---------------------------------------------------------------------------

AsserterRole = Literal["self", "third_party"]
PrecisionClass = Literal["exact_source", "spoken_approximation"]
ExtractionStatus = Literal["processed", "transient_discard", "pii_detected", "incomplete_capture"]
CommitmentStatus = Literal["open", "in_progress", "blocked", "done"]


# ---------------------------------------------------------------------------
# Fact
# ---------------------------------------------------------------------------

class Fact(BaseModel):
    """A stated property of an entity -- a deadline, a budget, a role, a
    preference."""

    entity_mention: str = Field(..., description="Raw text mention of the entity this fact is about.")
    entity_type: str = Field(..., description="Free-text kind of entity, e.g. 'project', 'person', 'system'.")
    attribute: str = Field(..., description="The property being stated, e.g. 'deadline', 'budget', 'owner'.")
    value_text: Optional[str] = None
    value_numeric: Optional[float] = None
    unit: Optional[str] = None
    precision_class: PrecisionClass
    asserter_role: AsserterRole
    relative_time_expression: Optional[str] = Field(
        default=None,
        description="Original spoken/selected phrase for a date-like value, e.g. 'next Tuesday'.",
    )
    resolved_time: Optional[str] = Field(
        default=None,
        description="Absolute ISO-8601 resolution of relative_time_expression, computed at ingestion.",
    )

    @model_validator(mode="after")
    def _require_a_value(self) -> "Fact":
        if self.value_text is None and self.value_numeric is None:
            raise ValueError(
                "Fact must set at least one of value_text or value_numeric -- "
                "an empty fact carries no information worth storing."
            )
        return self


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

class Event(BaseModel):
    """Something that happened at a point in time."""

    entity_mention: Optional[str] = Field(
        default=None, description="Entity this event concerns, if any. Some events are self-contained."
    )
    event_type: str = Field(
        ..., description="Free-text label, e.g. 'decision', 'problem_encountered', 'resolution_found'."
    )
    description: str = Field(..., description="What actually happened -- carries most of the informational weight.")
    relative_time_expression: Optional[str] = None
    resolved_time: Optional[str] = None
    asserter_role: Optional[AsserterRole] = None


# ---------------------------------------------------------------------------
# Commitment
# ---------------------------------------------------------------------------

class Commitment(BaseModel):
    """Anything planned, promised, or owed."""

    commitment_mention: str = Field(
        ..., description="Short raw-text label, used as the join key for later mentions/relationships."
    )
    description: str
    entity_mention: Optional[str] = None
    status: CommitmentStatus = "open"
    status_confirmed_by_user: bool = Field(
        default=False,
        description="True only for an explicit, unambiguous completion statement. Hedged language must never set this True.",
    )
    blocking_reason: Optional[str] = None
    due_date_relative_expression: Optional[str] = None
    due_date_resolved: Optional[str] = None

    @model_validator(mode="after")
    def _done_requires_confirmation(self) -> "Commitment":
        if self.status == "done" and not self.status_confirmed_by_user:
            raise ValueError(
                "status='done' requires status_confirmed_by_user=True -- "
                "never infer completion without an explicit user confirmation."
            )
        return self

    @model_validator(mode="after")
    def _blocked_requires_reason(self) -> "Commitment":
        if self.status == "blocked" and not self.blocking_reason:
            raise ValueError("status='blocked' requires a non-empty blocking_reason.")
        return self


# ---------------------------------------------------------------------------
# Preference
# ---------------------------------------------------------------------------

class Preference(BaseModel):
    """An enduring habit or operational constraint -- HOW the user wants
    something done, not a ground truth about the world (that's a Fact)."""

    preference_text: str = Field(
        ..., description="The standing instruction/habit itself, in the user's own terms."
    )
    entity_mention: Optional[str] = Field(
        default=None, description="Entity this preference is scoped to, if any. Most preferences are unscoped."
    )
    category: Optional[str] = Field(
        default=None, description="Free-text grouping, e.g. 'formatting', 'workflow', 'communication_style'."
    )

    @model_validator(mode="after")
    def _category_requires_entity_or_is_general(self) -> "Preference":
        # Not enforced. A category with no entity is still meaningful; this is just
        # a home for a stricter rule if one is ever wanted.
        return self


# ---------------------------------------------------------------------------
# Relationship
# ---------------------------------------------------------------------------

class Relationship(BaseModel):
    """Generic link between two mentions (facts, events, or commitments)."""

    source_mention: str
    target_mention: str
    relationship_type: str = Field(
        ..., description="Free-text kind of connection, e.g. 'must_precede', 'resolves', 'corrects'."
    )
    reason: Optional[str] = None

    @model_validator(mode="after")
    def _source_and_target_must_differ(self) -> "Relationship":
        if self.source_mention == self.target_mention:
            raise ValueError("A relationship's source_mention and target_mention must not be identical.")
        return self


# ---------------------------------------------------------------------------
# ExtractionResult
# ---------------------------------------------------------------------------

class ExtractionResult(BaseModel):
    """Top-level structured output of the ingestion LLM call for one capture."""

    capture_id: str
    extraction_status: ExtractionStatus
    discard_reason: Optional[str] = None
    facts: list[Fact] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    preferences: list[Preference] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)

    # Set only when the text names an app outright. None means "not stated",
    # never a guess.
    inferred_foreground_app: Optional[str] = None

    @model_validator(mode="after")
    def _discard_reason_required_when_not_processed(self) -> "ExtractionResult":
        if self.extraction_status != "processed" and not self.discard_reason:
            raise ValueError("discard_reason is required whenever extraction_status != 'processed'.")
        return self

    @model_validator(mode="after")
    def _no_content_leak_on_discard(self) -> "ExtractionResult":
        if self.extraction_status != "processed" and (
            self.facts or self.events or self.commitments or self.preferences or self.relationships
        ):
            raise ValueError(
                "A capture with extraction_status != 'processed' must not carry any "
                "facts/events/commitments/preferences/relationships -- a quarantined or "
                "discarded capture must never leak content into active memory."
            )
        return self
