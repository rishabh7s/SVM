"""
Pydantic v2 models for the ingestion extraction contract.

These map 1:1 to kivi_extraction_schema_v2.json (facts / events / commitments
/ relationships as generic primitives -- see that file's docstring for why
entity_type / event_type / relationship_type are open strings rather than
enums). Where the JSON Schema expressed a constraint as if/then, this file
enforces the same constraint with a Pydantic model_validator, so a violation
is caught the moment `instructor` tries to parse an LLM response into one of
these models -- before it ever reaches the database.

A few validators here go slightly beyond what the JSON Schema stated
literally, because Pydantic makes it cheap to enforce them and they close
gaps the JSON Schema left soft:
  - Fact must carry at least one of value_text / value_numeric.
  - Commitment status 'blocked' must carry a blocking_reason.
  - ExtractionResult must not carry any facts/events/commitments/relationships
    when extraction_status != 'processed' -- a quarantined capture must not
    leak content into memory just because the model still tried to extract
    something from it.
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
    preference. entity_type and attribute are free text on purpose; see the
    module docstring."""

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
    """Something that happened at a point in time. event_type is free text --
    see the module docstring for why."""

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
    """Anything planned, promised, or owed. Mirrors the database's
    commitments / commitment_status_events split conceptually: this model
    represents a single reported status at extraction time, not the full
    history -- the ingestion pipeline is responsible for writing it as a new
    versioned row rather than mutating an existing one."""

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
    something done, not a ground truth about the world (that's a Fact).
    e.g. "always summarize in bullet points" (unscoped: no entity_mention,
    no category), or "David prefers async updates over meetings"
    (entity_mention='David', category='communication_style').

    entity_mention/category are both optional together on purpose: a
    general standing preference has neither. When BOTH are present, this
    preference participates in supersession (see writer.py) the same way a
    Fact does for (entity_id, attribute); when either is missing, it is
    always appended fresh rather than deduplicated, since there is no
    reliable key to group repeats under.
    """

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
        # Deliberately NOT enforced as a hard requirement -- a category
        # without an entity_mention is still meaningful (e.g. a general
        # 'formatting' preference with category='formatting' but no
        # specific entity). Left as a plain pass-through; kept as an
        # explicit validator stub so a future stricter rule has a home.
        return self


# ---------------------------------------------------------------------------
# Relationship
# ---------------------------------------------------------------------------

class Relationship(BaseModel):
    """Generic link between two mentions (facts, events, or commitments).
    relationship_type is free text -- see the module docstring."""

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

    # Metadata inferred by the extractor about the capture itself, distinct
    # from the memory items above. Populated only when the source text
    # explicitly names an application ("In Slack...", "From Chrome...") --
    # see extractor.py's SYSTEM_PROMPT_TEMPLATE. None means "not stated,"
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
