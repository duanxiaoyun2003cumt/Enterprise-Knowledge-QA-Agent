"""Pydantic contracts for agent actions and final results.

Phase 1 defines the wire format only. Retrieval, tool dispatch, evidence
selection, and answer quality judgments belong to later phases.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
)


class TaskStatus(str, Enum):
    """Terminal status shared by ``finish`` actions and final results."""

    ANSWERED = "answered"
    NEEDS_CLARIFICATION = "needs_clarification"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    TOOL_ERROR = "tool_error"
    INCOMPLETE = "incomplete"


NonEmptyStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]
ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]
TopK = Annotated[int, Field(strict=True, ge=1, le=5)]


class Citation(BaseModel):
    """A verbatim excerpt supporting an answer."""

    model_config = ConfigDict(extra="forbid")

    doc_id: NonEmptyStr = Field(description="Identifier of the cited document.")
    quote: NonEmptyStr = Field(description="Verbatim excerpt supporting the answer.")
    version: NonEmptyStr | None = Field(
        default=None, description="Optional document version label."
    )
    effective_date: NonEmptyStr | None = Field(
        default=None, description="Optional effective date (ISO 8601 recommended)."
    )


class _Action(BaseModel):
    """Fields shared by every agent action."""

    model_config = ConfigDict(extra="forbid")

    decision_note: ShortText = Field(
        description="Short, reviewable justification for the chosen action."
    )


class SearchDocumentsAction(_Action):
    action: Literal["search_documents"]
    query: NonEmptyStr = Field(description="Search query.")
    top_k: TopK = Field(description="Maximum number of candidates to return (1-5).")


class ReadDocumentAction(_Action):
    action: Literal["read_document"]
    doc_id: NonEmptyStr = Field(description="Document to read.")


class AskUserAction(_Action):
    action: Literal["ask_user"]
    question: NonEmptyStr = Field(description="Clarifying question for the user.")
    reason: ShortText = Field(description="Why clarification is needed.")


class FinishAction(_Action):
    action: Literal["finish"]
    status: TaskStatus
    answer: NonEmptyStr = Field(description="Answer or status explanation.")
    citations: list[Citation] = Field(description="Sources supporting the answer.")
    gaps: list[NonEmptyStr] = Field(description="Known evidence gaps.")


AgentActionUnion: TypeAlias = (
    SearchDocumentsAction | ReadDocumentAction | AskUserAction | FinishAction
)
AgentAction: TypeAlias = Annotated[AgentActionUnion, Field(discriminator="action")]

_ACTION_ADAPTER = TypeAdapter(AgentAction)


def parse_agent_action(payload: Any) -> AgentActionUnion:
    """Validate JSON-compatible data as exactly one of the four agent actions."""
    return _ACTION_ADAPTER.validate_python(payload)


class FinalResult(BaseModel):
    """Final output returned to the caller once the agent finishes."""

    model_config = ConfigDict(extra="forbid")

    status: TaskStatus
    answer: NonEmptyStr
    citations: list[Citation]
    gaps: list[NonEmptyStr]
