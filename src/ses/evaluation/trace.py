"""Build and project immutable traces from canonical Engine events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pydantic import JsonValue, ValidationError

from ses.contracts import (
    CompletedPayload,
    EngineEvent,
    EngineRequest,
    RecordType,
    SchemaVersion,
    TextDeltaPayload,
    ToolCallPayload,
    ToolResultPayload,
    Trace,
    TraceId,
    UsagePayload,
)

from .errors import EvaluationError, EvaluationErrorCode, TraceBuildError


@dataclass(frozen=True, slots=True)
class TraceMessage:
    """A read-only message projection assembled from text-delta events."""

    message_id: str
    text: str
    event_ids: tuple[str, ...]
    sequences: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TraceToolCall:
    """A read-only tool-call projection joined to its canonical result."""

    event_index: int
    event_id: str
    sequence: int
    message_id: str
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, JsonValue]
    result: ToolResultPayload | None = None
    result_event_index: int | None = None


def build_trace(
    events: Iterable[EngineEvent],
    *,
    request: EngineRequest,
    run_id: str,
    case_id: str,
    iteration_id: str,
    trace_id: TraceId | None = None,
    skill_version: str | None = None,
    skill_sha256: str | None = None,
) -> Trace:
    """Construct a canonical Trace from already normalized Engine events."""

    canonical_events = tuple(events)
    if not all(isinstance(event, EngineEvent) for event in canonical_events):
        raise TypeError("events must contain canonical EngineEvent instances")
    if not canonical_events:
        raise TraceBuildError(
            EvaluationError(
                EvaluationErrorCode.INVALID_TRACE,
                "Trace requires at least one EngineEvent",
            )
        )
    terminal = canonical_events[-1].payload
    if not isinstance(terminal, CompletedPayload):
        raise TraceBuildError(
            EvaluationError(
                EvaluationErrorCode.INVALID_TRACE,
                "Trace events must end with a completed event",
            )
        )
    usage_events = tuple(
        event.payload.usage
        for event in canonical_events
        if isinstance(event.payload, UsagePayload)
    )
    try:
        return Trace(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.TRACE,
            trace_id=trace_id or f"trace-{run_id}-{case_id}-{iteration_id}",
            run_id=run_id,
            case_id=case_id,
            iteration_id=iteration_id,
            session_id=terminal.session_id,
            request=request,
            events=canonical_events,
            usage=usage_events[-1] if usage_events else None,
            exit_status=terminal.exit_status,
            skill_version=skill_version,
            skill_sha256=skill_sha256,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise TraceBuildError(
            EvaluationError(EvaluationErrorCode.INVALID_TRACE, str(exc))
        ) from exc


def trace_messages(trace: Trace) -> tuple[TraceMessage, ...]:
    """Group text chunks by message ID while retaining identity and order."""

    grouped: dict[str, list[tuple[str, int, str]]] = {}
    first_sequence: dict[str, int] = {}
    for event in trace.events:
        payload = event.payload
        if not isinstance(payload, TextDeltaPayload):
            continue
        grouped.setdefault(payload.message_id, []).append(
            (event.event_id, event.sequence, payload.text)
        )
        first_sequence.setdefault(payload.message_id, event.sequence)
    message_ids = sorted(grouped, key=first_sequence.__getitem__)
    return tuple(
        TraceMessage(
            message_id=message_id,
            text="".join(item[2] for item in grouped[message_id]),
            event_ids=tuple(item[0] for item in grouped[message_id]),
            sequences=tuple(item[1] for item in grouped[message_id]),
        )
        for message_id in message_ids
    )


def trace_tool_calls(trace: Trace) -> tuple[TraceToolCall, ...]:
    """Return calls in event order, joined to results by tool_call_id."""

    results: dict[str, tuple[int, ToolResultPayload]] = {}
    for index, event in enumerate(trace.events):
        if isinstance(event.payload, ToolResultPayload):
            results.setdefault(event.payload.tool_call_id, (index, event.payload))
    calls: list[TraceToolCall] = []
    for index, event in enumerate(trace.events):
        payload = event.payload
        if not isinstance(payload, ToolCallPayload):
            continue
        result = results.get(payload.tool_call_id)
        calls.append(
            TraceToolCall(
                event_index=index,
                event_id=event.event_id,
                sequence=event.sequence,
                message_id=payload.message_id,
                tool_call_id=payload.tool_call_id,
                tool_name=payload.tool_name,
                arguments=payload.arguments,
                result=result[1] if result else None,
                result_event_index=result[0] if result else None,
            )
        )
    return tuple(calls)
