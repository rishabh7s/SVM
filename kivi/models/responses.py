"""
Pydantic v2 models for the interrogation agent's response contract.

The shape-matches-type validator is the important part of this file: it
enforces at the model level that an 'answer' response must be cited (an
uncited answer is a modeling bug, not a valid answer -- it should have been
an abstention), an 'abstain' response must explain why, and a
'needs_disambiguation' response must actually offer a choice. This makes it
structurally impossible for the agent to return a confident-sounding answer
with no backing citation, regardless of what the underlying LLM call
produces -- the validation itself is the safety net, not a prompt
instruction that the model might ignore.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

SourceType = Literal["fact", "event", "commitment"]
ResponseType = Literal["answer", "abstain", "needs_disambiguation"]


class Citation(BaseModel):
    """Points back to exactly the record(s) that justify an answer."""

    source_type: SourceType
    source_id: str = Field(..., description="Primary key of the cited row, e.g. a fact_id or event_id.")
    snippet: str = Field(..., description="Short excerpt or paraphrase of the cited record, shown to the user.")


class AgentResponse(BaseModel):
    response_type: ResponseType
    derived_answer: Optional[str] = Field(
        default=None,
        description="The answer text, populated only when response_type == 'answer'.",
    )
    abstain_reason: Optional[str] = Field(
        default=None,
        description="Why the agent is abstaining, populated only when response_type == 'abstain'.",
    )
    disambiguation_options: Optional[List[str]] = Field(
        default=None,
        description="Choices to present to the user, populated only when response_type == 'needs_disambiguation'.",
    )
    citations: List[Citation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape_must_match_response_type(self) -> "AgentResponse":
        if self.response_type == "answer":
            if not self.derived_answer:
                raise ValueError("response_type='answer' requires derived_answer to be set.")
            if not self.citations:
                raise ValueError(
                    "response_type='answer' requires at least one citation -- "
                    "an answer the agent cannot cite should be an abstention instead."
                )
        elif self.response_type == "abstain":
            if not self.abstain_reason:
                raise ValueError("response_type='abstain' requires abstain_reason to be set.")
        elif self.response_type == "needs_disambiguation":
            if not self.disambiguation_options or len(self.disambiguation_options) < 2:
                raise ValueError(
                    "response_type='needs_disambiguation' requires at least two disambiguation_options."
                )
        return self
