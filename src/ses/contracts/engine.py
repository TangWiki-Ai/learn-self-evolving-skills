"""Provider-neutral engine request and event contracts."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, field_validator, model_validator

from ses.contracts.base import (
    ArtifactRef,
    ContractModel,
    CurrencyCode,
    EventId,
    MessageId,
    NonEmptyStr,
    RecordType,
    RequestId,
    SessionId,
    StrictNonNegativeInt,
    ToolCallId,
    UtcDateTime,
    VersionedRecord,
)


class EngineEventKind(StrEnum):
    """Normalized event shapes emitted by every engine adapter."""

    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    ERROR = "error"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class EngineExitStatus(StrEnum):
    """Terminal engine outcomes kept separate from grading outcomes."""

    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    BUDGET_STOP = "budget_stop"


class Usage(ContractModel):
    """Token counts and optional provider-neutral model cost."""

    input_tokens: StrictNonNegativeInt
    output_tokens: StrictNonNegativeInt
    cost_amount: Decimal | None = None
    cost_currency: CurrencyCode | None = None

    @field_validator("cost_amount", mode="before")
    @classmethod
    def _require_decimal_input(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, Decimal)):
            raise ValueError("cost_amount must be a decimal string")
        return value

    @model_validator(mode="after")
    def _validate_cost(self) -> Usage:
        if (self.cost_amount is None) != (self.cost_currency is None):
            raise ValueError("cost_amount and cost_currency must be provided together")
        if self.cost_amount is not None:
            if not self.cost_amount.is_finite() or self.cost_amount < 0:
                raise ValueError("cost_amount must be a finite nonnegative decimal")
        return self


class EngineRequest(VersionedRecord):
    """Narrow input accepted by engine adapters."""

    record_type: Literal[RecordType.ENGINE_REQUEST] = RecordType.ENGINE_REQUEST
    request_id: RequestId
    prompt: NonEmptyStr
    resume_session_id: SessionId | None = None
    allowed_tools: tuple[NonEmptyStr, ...] = ()
    timeout_seconds: Annotated[float, Field(gt=0)]

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _require_numeric_timeout(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("timeout_seconds must be a number")
        return value

    @field_validator("allowed_tools")
    @classmethod
    def _reject_duplicate_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_tools must not contain duplicates")
        return value


class TextDeltaPayload(ContractModel):
    """A streamed assistant text fragment."""

    kind: Literal[EngineEventKind.TEXT_DELTA] = EngineEventKind.TEXT_DELTA
    message_id: MessageId
    text: NonEmptyStr


class ToolCallPayload(ContractModel):
    """A normalized tool request."""

    kind: Literal[EngineEventKind.TOOL_CALL] = EngineEventKind.TOOL_CALL
    message_id: MessageId
    tool_call_id: ToolCallId
    tool_name: NonEmptyStr
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResultPayload(ContractModel):
    """A normalized tool response observed by the engine."""

    kind: Literal[EngineEventKind.TOOL_RESULT] = EngineEventKind.TOOL_RESULT
    tool_call_id: ToolCallId
    content: JsonValue
    is_error: bool


class UsagePayload(ContractModel):
    """Cumulative request usage as of this event's sequence."""

    kind: Literal[EngineEventKind.USAGE] = EngineEventKind.USAGE
    usage: Usage


class ErrorPayload(ContractModel):
    """A structured engine or stream error."""

    kind: Literal[EngineEventKind.ERROR] = EngineEventKind.ERROR
    error_code: NonEmptyStr
    message: NonEmptyStr


class CompletedPayload(ContractModel):
    """The terminal engine event."""

    kind: Literal[EngineEventKind.COMPLETED] = EngineEventKind.COMPLETED
    exit_status: EngineExitStatus
    session_id: SessionId | None = None


class UnknownPayload(ContractModel):
    """An ordered placeholder for an unrecognized non-critical event."""

    kind: Literal[EngineEventKind.UNKNOWN] = EngineEventKind.UNKNOWN
    source_type: NonEmptyStr
    artifact: ArtifactRef | None = None


EngineEventPayload: TypeAlias = Annotated[
    TextDeltaPayload
    | ToolCallPayload
    | ToolResultPayload
    | UsagePayload
    | ErrorPayload
    | CompletedPayload
    | UnknownPayload,
    Field(discriminator="kind"),
]


class EngineEvent(VersionedRecord):
    """One normalized, strictly ordered engine observation."""

    canonical_exclude = frozenset({"occurred_at"})

    record_type: Literal[RecordType.ENGINE_EVENT] = RecordType.ENGINE_EVENT
    event_id: EventId
    request_id: RequestId
    sequence: StrictNonNegativeInt
    occurred_at: UtcDateTime
    payload: EngineEventPayload
