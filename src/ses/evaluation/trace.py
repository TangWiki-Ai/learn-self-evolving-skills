"""Normalize stream-json observations and build the canonical immutable Trace."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TypeAlias, cast

from pydantic import JsonValue, ValidationError

from ses.contracts import (
    CompletedPayload,
    EngineEvent,
    EngineEventKind,
    EngineExitStatus,
    EngineRequest,
    RecordType,
    SchemaVersion,
    TextDeltaPayload,
    ToolCallPayload,
    ToolResultPayload,
    Trace,
    TraceId,
    Usage,
    UsagePayload,
)

from .errors import EvaluationError, EvaluationErrorCode, TraceParseError

DEFAULT_OCCURRED_AT = "1970-01-01T00:00:00Z"

RawMapping: TypeAlias = Mapping[str, object]
RawItem: TypeAlias = str | bytes | RawMapping
RawSource: TypeAlias = str | bytes | Iterable[RawItem]


@dataclass(frozen=True, slots=True)
class TraceParseResult:
    """A parser outcome that retains a valid Trace or a safe structured error."""

    trace: Trace | None
    events: tuple[EngineEvent, ...]
    error: EvaluationError | None = None

    @property
    def ok(self) -> bool:
        return self.trace is not None and self.error is None

    def unwrap(self) -> Trace:
        """Return the Trace or raise the explicit parser exception."""

        if self.trace is None or self.error is not None:
            error = self.error or EvaluationError(
                EvaluationErrorCode.INVALID_TRACE,
                "the parser did not produce a complete Trace",
            )
            raise TraceParseError(error)
        return self.trace


@dataclass(frozen=True, slots=True)
class TraceMessage:
    """A read-only message projection assembled from text-delta events."""

    message_id: str
    text: str
    event_ids: tuple[str, ...]
    sequences: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TraceToolCall:
    """A read-only tool-call projection assembled from a Trace."""

    event_index: int
    event_id: str
    sequence: int
    message_id: str
    tool_call_id: str
    tool_name: str
    arguments: Mapping[str, JsonValue]
    result: ToolResultPayload | None = None
    result_event_index: int | None = None


@dataclass(frozen=True, slots=True)
class _CriticalEventFailure(Exception):
    message: str
    event_type: str | None = None

    def __str__(self) -> str:
        return self.message


def _is_non_blank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _mapping(value: object) -> RawMapping | None:
    return value if isinstance(value, Mapping) else None


def _string(value: object, *, default: str | None = None) -> str | None:
    if _is_non_blank_string(value):
        return cast(str, value)
    return default


def _event_type(raw: RawMapping) -> str:
    value = raw.get("type")
    if not _is_non_blank_string(value):
        value = raw.get("kind")
    if not _is_non_blank_string(value):
        raise _CriticalEventFailure("stream event is missing a non-empty type")
    return cast(str, value)


def _stable_raw_digest(raw: RawMapping) -> str:
    try:
        encoded = json.dumps(
            raw,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError):
        encoded = repr(
            sorted((str(key), repr(value)) for key, value in raw.items())
        ).encode(
            "utf-8",
            errors="replace",
        )
    return hashlib.sha256(encoded).hexdigest()[:20]


def _raw_event_id(raw: RawMapping, event_type: str) -> str:
    for key in ("event_id", "uuid", "id", "message_id"):
        candidate = _string(raw.get(key))
        if candidate is not None:
            return candidate
    return f"event-{event_type}-{_stable_raw_digest(raw)}"


def _occurred_at(raw: RawMapping) -> str | datetime:
    for key in ("occurred_at", "timestamp", "created_at"):
        value = raw.get(key)
        if isinstance(value, datetime):
            return value
        if _is_non_blank_string(value):
            return cast(str, value)
    return DEFAULT_OCCURRED_AT


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _CriticalEventFailure(f"{field_name} must be a nonnegative integer")
    return value


def _usage(raw: RawMapping, *, event_type: str) -> Usage:
    input_tokens = raw.get("input_tokens", raw.get("inputTokens"))
    output_tokens = raw.get("output_tokens", raw.get("outputTokens"))
    if input_tokens is None or output_tokens is None:
        nested = _mapping(raw.get("usage"))
        if nested is not None:
            input_tokens = nested.get("input_tokens", nested.get("inputTokens"))
            output_tokens = nested.get("output_tokens", nested.get("outputTokens"))
            raw = nested
    if input_tokens is None or output_tokens is None:
        raise _CriticalEventFailure(
            "usage event is missing input_tokens or output_tokens", event_type
        )
    data: dict[str, object] = {
        "input_tokens": _nonnegative_int(input_tokens, "input_tokens"),
        "output_tokens": _nonnegative_int(output_tokens, "output_tokens"),
    }
    amount = raw.get("cost_amount", raw.get("total_cost_usd"))
    currency = raw.get("cost_currency")
    if (
        amount is not None
        and currency is None
        and raw.get("total_cost_usd") is not None
    ):
        currency = "USD"
    if amount is not None or currency is not None:
        if not isinstance(amount, (str, Decimal)) or not _is_non_blank_string(currency):
            raise _CriticalEventFailure(
                "usage cost requires a decimal amount and currency", event_type
            )
        data["cost_amount"] = amount
        data["cost_currency"] = currency
    try:
        return Usage.model_validate(data)
    except ValidationError as exc:
        raise _CriticalEventFailure("usage payload is invalid", event_type) from exc


def _content_blocks(value: object) -> tuple[RawMapping, ...]:
    if isinstance(value, str):
        return ({"type": "text", "text": value},)
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        blocks = tuple(item for item in value if isinstance(item, Mapping))
        return cast(tuple[RawMapping, ...], blocks)
    return ()


def _session_id(raw: RawMapping) -> str | None:
    candidate = _string(raw.get("session_id"))
    if candidate is not None:
        return candidate
    message = _mapping(raw.get("message"))
    if message is not None:
        return _string(message.get("session_id"))
    return None


def _json_value(value: object) -> JsonValue:
    """Cast a JSON-shaped provider value after canonical validation does the check."""

    return cast(JsonValue, value)


def _exit_status(raw: RawMapping, *, process_exit_code: int | None) -> EngineExitStatus:
    if process_exit_code is not None and process_exit_code != 0:
        return EngineExitStatus.ERROR
    raw_status = _string(raw.get("exit_status"))
    if raw_status is None:
        subtype = (_string(raw.get("subtype")) or "").lower()
        if subtype in {"timeout", "timed_out"}:
            return EngineExitStatus.TIMEOUT
        if subtype in {"cancelled", "canceled"}:
            return EngineExitStatus.CANCELLED
        if subtype in {"budget_stop", "budget_exceeded", "max_budget_usd"}:
            return EngineExitStatus.BUDGET_STOP
        if raw.get("is_error") is True or subtype.startswith("error"):
            return EngineExitStatus.ERROR
        return EngineExitStatus.SUCCESS
    normalized = raw_status.lower()
    mapping = {
        "success": EngineExitStatus.SUCCESS,
        "completed": EngineExitStatus.SUCCESS,
        "error": EngineExitStatus.ERROR,
        "failed": EngineExitStatus.ERROR,
        "timeout": EngineExitStatus.TIMEOUT,
        "timed_out": EngineExitStatus.TIMEOUT,
        "cancelled": EngineExitStatus.CANCELLED,
        "canceled": EngineExitStatus.CANCELLED,
        "budget_stop": EngineExitStatus.BUDGET_STOP,
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise _CriticalEventFailure(
            f"unsupported exit status {raw_status!r}", "result"
        ) from exc


@dataclass
class _Normalizer:
    request: EngineRequest
    events: list[EngineEvent] = field(default_factory=list)
    used_event_ids: set[str] = field(default_factory=set)
    next_sequence: int = 0
    last_input_sequence: int | None = None
    session_id: str | None = None
    saw_text: bool = False

    def _event_id(self, base: str, suffix: str | None) -> str:
        candidate = base if suffix is None else f"{base}-{suffix}"
        if candidate not in self.used_event_ids:
            self.used_event_ids.add(candidate)
            return candidate
        counter = 1
        while f"{candidate}-{counter}" in self.used_event_ids:
            counter += 1
        result = f"{candidate}-{counter}"
        self.used_event_ids.add(result)
        return result

    def emit(
        self,
        payload: Mapping[str, object],
        *,
        raw: RawMapping,
        base_event_id: str,
        suffix: str | None = None,
        sequence_hint: int | None = None,
    ) -> EngineEvent:
        if sequence_hint is not None and sequence_hint >= self.next_sequence:
            sequence = sequence_hint
        else:
            sequence = self.next_sequence
        self.next_sequence = sequence + 1
        data = {
            "schema_version": "v1alpha1",
            "record_type": RecordType.ENGINE_EVENT,
            "event_id": self._event_id(base_event_id, suffix),
            "request_id": self.request.request_id,
            "sequence": sequence,
            "occurred_at": _occurred_at(raw),
            "payload": dict(payload),
        }
        try:
            event = EngineEvent.model_validate(data)
        except ValidationError as exc:
            raise _CriticalEventFailure("normalized engine event is invalid") from exc
        self.events.append(event)
        if isinstance(event.payload, TextDeltaPayload):
            self.saw_text = True
        if isinstance(event.payload, CompletedPayload):
            self.session_id = event.payload.session_id
        return event

    def emit_unknown(
        self,
        raw: RawMapping,
        *,
        event_type: str,
        base_event_id: str,
        sequence_hint: int | None = None,
    ) -> None:
        subtype = _string(raw.get("subtype"))
        source_type = event_type if subtype is None else f"{event_type}:{subtype}"
        self.emit(
            {
                "kind": EngineEventKind.UNKNOWN,
                "source_type": source_type,
            },
            raw=raw,
            base_event_id=base_event_id,
            sequence_hint=sequence_hint,
        )


def _sequence_hint(raw: RawMapping) -> int | None:
    value = raw.get("sequence")
    if value is None:
        return None
    return _nonnegative_int(value, "sequence")


def _emit_text(
    normalizer: _Normalizer,
    raw: RawMapping,
    text: object,
    *,
    message_id: str,
    base_event_id: str,
    suffix: str | None,
    sequence_hint: int | None,
) -> None:
    if not _is_non_blank_string(text):
        raise _CriticalEventFailure("text event is missing non-empty text")
    normalizer.emit(
        {
            "kind": EngineEventKind.TEXT_DELTA,
            "message_id": message_id,
            "text": text,
        },
        raw=raw,
        base_event_id=base_event_id,
        suffix=suffix,
        sequence_hint=sequence_hint,
    )


def _normalize_assistant(
    normalizer: _Normalizer,
    raw: RawMapping,
    *,
    event_type: str,
    base_event_id: str,
    sequence_hint: int | None,
) -> None:
    message = _mapping(raw.get("message")) or raw
    message_id = _string(message.get("id")) or base_event_id
    content = message.get("content", raw.get("content"))
    blocks = _content_blocks(content)
    if not blocks:
        delta = _mapping(raw.get("delta"))
        if delta is not None:
            blocks = (delta,)
    if not blocks:
        normalizer.emit_unknown(
            raw,
            event_type=event_type,
            base_event_id=base_event_id,
            sequence_hint=sequence_hint,
        )
        return
    for index, block in enumerate(blocks):
        block_type = _string(block.get("type"))
        suffix = None if len(blocks) == 1 else str(index)
        if block_type in {"text", "text_delta"}:
            _emit_text(
                normalizer,
                raw,
                block.get("text"),
                message_id=message_id,
                base_event_id=base_event_id,
                suffix=suffix,
                sequence_hint=sequence_hint if index == 0 else None,
            )
        elif block_type == "tool_use":
            tool_call_id = _string(block.get("id"))
            tool_name = _string(block.get("name"))
            if tool_call_id is None or tool_name is None:
                raise _CriticalEventFailure(
                    "tool_use event is missing id or name", event_type
                )
            arguments = block.get("input", block.get("arguments", {}))
            if not isinstance(arguments, Mapping):
                raise _CriticalEventFailure(
                    "tool_use input must be a JSON object", event_type
                )
            normalizer.emit(
                {
                    "kind": EngineEventKind.TOOL_CALL,
                    "message_id": message_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": _json_value(arguments),
                },
                raw=raw,
                base_event_id=base_event_id,
                suffix=suffix,
                sequence_hint=sequence_hint if index == 0 else None,
            )
        else:
            normalizer.emit_unknown(
                block,
                event_type=f"{event_type}:{block_type or 'unknown_block'}",
                base_event_id=base_event_id,
                sequence_hint=sequence_hint if index == 0 else None,
            )


def _normalize_tool_results(
    normalizer: _Normalizer,
    raw: RawMapping,
    *,
    event_type: str,
    base_event_id: str,
    sequence_hint: int | None,
) -> None:
    message = _mapping(raw.get("message")) or raw
    content = message.get("content", raw.get("content"))
    blocks = _content_blocks(content)
    if event_type == "tool_result" and raw.get("tool_use_id") is not None:
        blocks = (raw,)
    result_blocks = tuple(
        block for block in blocks if block.get("type") == "tool_result"
    )
    if not result_blocks:
        normalizer.emit_unknown(
            raw,
            event_type=event_type,
            base_event_id=base_event_id,
            sequence_hint=sequence_hint,
        )
        return
    for index, block in enumerate(result_blocks):
        tool_call_id = _string(block.get("tool_use_id", block.get("tool_call_id")))
        if tool_call_id is None:
            raise _CriticalEventFailure(
                "tool_result is missing tool_use_id", event_type
            )
        is_error = block.get("is_error", False)
        if not isinstance(is_error, bool):
            raise _CriticalEventFailure(
                "tool_result is_error must be boolean", event_type
            )
        normalizer.emit(
            {
                "kind": EngineEventKind.TOOL_RESULT,
                "tool_call_id": tool_call_id,
                "content": _json_value(block.get("content")),
                "is_error": is_error,
            },
            raw=raw,
            base_event_id=base_event_id,
            suffix=None if len(result_blocks) == 1 else str(index),
            sequence_hint=sequence_hint if index == 0 else None,
        )


def _normalize_result(
    normalizer: _Normalizer,
    raw: RawMapping,
    *,
    base_event_id: str,
    sequence_hint: int | None,
    process_exit_code: int | None,
) -> None:
    status = _exit_status(raw, process_exit_code=process_exit_code)
    session_id = _session_id(raw) or normalizer.session_id
    result_text = raw.get("result")
    if not normalizer.saw_text and _is_non_blank_string(result_text):
        _emit_text(
            normalizer,
            raw,
            result_text,
            message_id=_string(raw.get("message_id")) or base_event_id,
            base_event_id=base_event_id,
            suffix="text",
            sequence_hint=sequence_hint,
        )
        sequence_hint = None

    usage_raw = _mapping(raw.get("usage"))
    if usage_raw is not None:
        combined_usage = dict(usage_raw)
        for key in ("cost_amount", "cost_currency", "total_cost_usd"):
            if key in raw:
                combined_usage[key] = raw[key]
        usage = _usage(combined_usage, event_type="result")
        normalizer.emit(
            {
                "kind": EngineEventKind.USAGE,
                "usage": usage.model_dump(mode="python"),
            },
            raw=raw,
            base_event_id=base_event_id,
            suffix="usage",
            sequence_hint=sequence_hint,
        )
        sequence_hint = None
    elif raw.get("input_tokens") is not None or raw.get("output_tokens") is not None:
        usage = _usage(raw, event_type="result")
        normalizer.emit(
            {
                "kind": EngineEventKind.USAGE,
                "usage": usage.model_dump(mode="python"),
            },
            raw=raw,
            base_event_id=base_event_id,
            suffix="usage",
            sequence_hint=sequence_hint,
        )
        sequence_hint = None
    normalizer.emit(
        {
            "kind": EngineEventKind.COMPLETED,
            "exit_status": status,
            "session_id": session_id,
        },
        raw=raw,
        base_event_id=base_event_id,
        suffix="completed" if usage_raw is not None else None,
        sequence_hint=sequence_hint,
    )


def _normalize_raw_event(
    normalizer: _Normalizer,
    raw: RawMapping,
    *,
    process_exit_code: int | None,
) -> None:
    event_type = _event_type(raw)
    base_event_id = _raw_event_id(raw, event_type)
    sequence_hint = _sequence_hint(raw)
    if sequence_hint is not None:
        if (
            normalizer.last_input_sequence is not None
            and sequence_hint <= normalizer.last_input_sequence
        ):
            raise _CriticalEventFailure(
                "stream event sequences must be strictly increasing", event_type
            )
        normalizer.last_input_sequence = sequence_hint
    session_id = _session_id(raw)
    if session_id is not None:
        normalizer.session_id = session_id

    if raw.get("record_type") == RecordType.ENGINE_EVENT.value:
        try:
            event = EngineEvent.model_validate(raw)
        except ValidationError as exc:
            raise _CriticalEventFailure(
                "canonical EngineEvent is malformed", event_type
            ) from exc
        if event.request_id != normalizer.request.request_id:
            raise _CriticalEventFailure(
                "canonical EngineEvent references a different request", event_type
            )
        if event.event_id in normalizer.used_event_ids:
            raise _CriticalEventFailure(
                "canonical EngineEvent ID is duplicated", event_type
            )
        normalizer.used_event_ids.add(event.event_id)
        normalizer.events.append(event)
        normalizer.next_sequence = max(normalizer.next_sequence, event.sequence + 1)
        if isinstance(event.payload, TextDeltaPayload):
            normalizer.saw_text = True
        if isinstance(event.payload, CompletedPayload):
            normalizer.session_id = event.payload.session_id
        return

    if event_type in {
        "assistant",
        "message",
        "content_block_start",
        "content_block_delta",
    }:
        if event_type == "content_block_start":
            block = _mapping(raw.get("content_block"))
            if block is None:
                raise _CriticalEventFailure(
                    "content_block_start is missing content_block", event_type
                )
            assistant_raw = dict(raw)
            assistant_raw["message"] = {
                "id": _string(raw.get("message_id")) or base_event_id,
                "content": (block,),
            }
            _normalize_assistant(
                normalizer,
                assistant_raw,
                event_type=event_type,
                base_event_id=base_event_id,
                sequence_hint=sequence_hint,
            )
        elif event_type == "content_block_delta":
            delta = _mapping(raw.get("delta"))
            if delta is None:
                raise _CriticalEventFailure(
                    "content_block_delta is missing delta", event_type
                )
            if _string(delta.get("type")) in {"text_delta", "text"}:
                _emit_text(
                    normalizer,
                    raw,
                    delta.get("text"),
                    message_id=_string(raw.get("message_id")) or base_event_id,
                    base_event_id=base_event_id,
                    suffix=None,
                    sequence_hint=sequence_hint,
                )
            else:
                normalizer.emit_unknown(
                    raw,
                    event_type=event_type,
                    base_event_id=base_event_id,
                    sequence_hint=sequence_hint,
                )
        else:
            _normalize_assistant(
                normalizer,
                raw,
                event_type=event_type,
                base_event_id=base_event_id,
                sequence_hint=sequence_hint,
            )
        return

    if event_type in {"user", "tool_result"}:
        _normalize_tool_results(
            normalizer,
            raw,
            event_type=event_type,
            base_event_id=base_event_id,
            sequence_hint=sequence_hint,
        )
        return

    if event_type in {"usage", "message_start", "message_delta"}:
        usage_raw = _mapping(raw.get("usage")) or raw
        if (
            usage_raw.get("input_tokens") is not None
            or usage_raw.get("output_tokens") is not None
            or usage_raw.get("inputTokens") is not None
            or usage_raw.get("outputTokens") is not None
        ):
            usage = _usage(usage_raw, event_type=event_type)
            normalizer.emit(
                {
                    "kind": EngineEventKind.USAGE,
                    "usage": usage.model_dump(mode="python"),
                },
                raw=raw,
                base_event_id=base_event_id,
                sequence_hint=sequence_hint,
            )
        else:
            normalizer.emit_unknown(
                raw,
                event_type=event_type,
                base_event_id=base_event_id,
                sequence_hint=sequence_hint,
            )
        return

    if event_type == "error":
        code = _string(raw.get("error_code", raw.get("code"))) or "stream_error"
        message = _string(raw.get("message"))
        if message is None:
            nested = _mapping(raw.get("error"))
            message = _string(nested.get("message")) if nested is not None else None
        normalizer.emit(
            {
                "kind": EngineEventKind.ERROR,
                "error_code": code,
                "message": message or "engine stream reported an error",
            },
            raw=raw,
            base_event_id=base_event_id,
            sequence_hint=sequence_hint,
        )
        return

    if event_type == "result":
        _normalize_result(
            normalizer,
            raw,
            base_event_id=base_event_id,
            sequence_hint=sequence_hint,
            process_exit_code=process_exit_code,
        )
        return

    normalizer.emit_unknown(
        raw,
        event_type=event_type,
        base_event_id=base_event_id,
        sequence_hint=sequence_hint,
    )


def _source_items(source: RawSource) -> tuple[RawItem, ...]:
    if isinstance(source, Mapping):
        return (source,)
    if isinstance(source, bytes):
        try:
            return tuple(source.decode("utf-8").splitlines())
        except UnicodeDecodeError as exc:
            raise _CriticalEventFailure("stream is not valid UTF-8") from exc
    if isinstance(source, str):
        return tuple(source.splitlines())
    return tuple(source)


def _raw_mappings(source: RawSource) -> tuple[tuple[int, RawMapping], ...]:
    result: list[tuple[int, RawMapping]] = []
    try:
        items = _source_items(source)
    except _CriticalEventFailure:
        raise
    for line_number, item in enumerate(items, start=1):
        if isinstance(item, bytes):
            try:
                item = item.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _CriticalEventFailure("stream line is not valid UTF-8") from exc
        if isinstance(item, str):
            if not item.strip():
                continue
            try:
                decoded = json.loads(item)
            except json.JSONDecodeError as exc:
                raise _CriticalEventFailure("stream line is not valid JSON") from exc
            if not isinstance(decoded, Mapping):
                raise _CriticalEventFailure("stream line must be a JSON object")
            result.append((line_number, decoded))
        elif isinstance(item, Mapping):
            result.append((line_number, item))
        else:
            raise _CriticalEventFailure("stream item must be JSON text or object")
    return tuple(result)


def _trace_from_events(
    events: tuple[EngineEvent, ...],
    *,
    request: EngineRequest,
    trace_id: TraceId,
    run_id: str,
    case_id: str,
    iteration_id: str,
    skill_version: str | None,
    skill_sha256: str | None,
) -> Trace:
    if not events:
        raise ValueError("Trace requires at least one EngineEvent")
    terminal = events[-1].payload
    if not isinstance(terminal, CompletedPayload):
        raise ValueError("Trace events must end with a completed event")
    usage_payloads = [
        event.payload.usage
        for event in events
        if isinstance(event.payload, UsagePayload)
    ]
    return Trace(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.TRACE,
        trace_id=trace_id,
        run_id=run_id,
        case_id=case_id,
        iteration_id=iteration_id,
        session_id=terminal.session_id,
        request=request,
        events=events,
        usage=usage_payloads[-1] if usage_payloads else None,
        exit_status=terminal.exit_status,
        skill_version=skill_version,
        skill_sha256=skill_sha256,
    )


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
    """Construct a canonical Trace from already normalized EngineEvents."""

    normalized = tuple(events)
    try:
        return _trace_from_events(
            normalized,
            request=request,
            trace_id=trace_id or f"trace-{run_id}-{case_id}-{iteration_id}",
            run_id=run_id,
            case_id=case_id,
            iteration_id=iteration_id,
            skill_version=skill_version,
            skill_sha256=skill_sha256,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise TraceParseError(
            EvaluationError(EvaluationErrorCode.INVALID_TRACE, str(exc))
        ) from exc


def parse_stream_json(
    source: RawSource | Iterable[EngineEvent],
    *,
    request: EngineRequest,
    run_id: str,
    case_id: str,
    iteration_id: str,
    trace_id: TraceId | None = None,
    process_exit_code: int | None = 0,
    skill_version: str | None = None,
    skill_sha256: str | None = None,
) -> TraceParseResult:
    """Parse raw stream-json or normalized events without guessing past failures."""

    if isinstance(source, (str, bytes, Mapping)):
        normalized_events: tuple[object, ...] = ()
        raw_source: RawSource = source
    else:
        normalized_events = tuple(source)
        raw_source = cast(RawSource, normalized_events)
    if normalized_events and all(
        isinstance(item, EngineEvent) for item in normalized_events
    ):
        try:
            trace = _trace_from_events(
                cast(tuple[EngineEvent, ...], normalized_events),
                request=request,
                trace_id=trace_id or f"trace-{run_id}-{case_id}-{iteration_id}",
                run_id=run_id,
                case_id=case_id,
                iteration_id=iteration_id,
                skill_version=skill_version,
                skill_sha256=skill_sha256,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            return TraceParseResult(
                trace=None,
                events=cast(tuple[EngineEvent, ...], normalized_events),
                error=EvaluationError(EvaluationErrorCode.INVALID_TRACE, str(exc)),
            )
        return TraceParseResult(trace=trace, events=trace.events)

    normalizer = _Normalizer(request=request)
    parse_error: EvaluationError | None = None
    try:
        raw_items = _raw_mappings(raw_source)
        if not raw_items:
            raise _CriticalEventFailure("stream contains no JSON events")
        for line_number, raw in raw_items:
            try:
                _normalize_raw_event(
                    normalizer,
                    raw,
                    process_exit_code=process_exit_code,
                )
            except _CriticalEventFailure as exc:
                if "truncated" in str(exc).lower():
                    code = EvaluationErrorCode.TRUNCATED_STREAM
                else:
                    code = EvaluationErrorCode.MALFORMED_CRITICAL_EVENT
                parse_error = EvaluationError(
                    code,
                    str(exc),
                    line_number=line_number,
                    event_type=exc.event_type,
                )
                break
        if (
            parse_error is None
            and process_exit_code is not None
            and process_exit_code != 0
        ):
            if normalizer.events and isinstance(
                normalizer.events[-1].payload, CompletedPayload
            ):
                normalizer.events.pop()
            error_raw: RawMapping = {
                "type": "error",
                "occurred_at": DEFAULT_OCCURRED_AT,
            }
            normalizer.emit(
                {
                    "kind": EngineEventKind.ERROR,
                    "error_code": "process_exit",
                    "message": "engine process exited with a non-zero status",
                },
                raw=error_raw,
                base_event_id="process-exit",
            )
            normalizer.emit(
                {
                    "kind": EngineEventKind.COMPLETED,
                    "exit_status": EngineExitStatus.ERROR,
                    "session_id": normalizer.session_id,
                },
                raw=error_raw,
                base_event_id="process-exit",
                suffix="completed",
            )
            parse_error = EvaluationError(
                EvaluationErrorCode.NON_ZERO_EXIT,
                "engine process exited with a non-zero status",
            )
        if parse_error is None and not normalizer.events:
            parse_error = EvaluationError(
                EvaluationErrorCode.TRUNCATED_STREAM,
                "stream contains no normalized events",
            )
        if parse_error is None and not isinstance(
            normalizer.events[-1].payload, CompletedPayload
        ):
            code = (
                EvaluationErrorCode.TERMINAL_EVENT_NOT_LAST
                if any(
                    isinstance(event.payload, CompletedPayload)
                    for event in normalizer.events
                )
                else EvaluationErrorCode.TRUNCATED_STREAM
            )
            parse_error = EvaluationError(
                code, "stream did not end with completed event"
            )
        if parse_error is None:
            completed_indexes = [
                index
                for index, event in enumerate(normalizer.events)
                if isinstance(event.payload, CompletedPayload)
            ]
            if len(completed_indexes) != 1:
                parse_error = EvaluationError(
                    EvaluationErrorCode.INVALID_TRACE,
                    "stream must contain exactly one completed event",
                )
    except _CriticalEventFailure as exc:
        parse_error = EvaluationError(
            EvaluationErrorCode.INVALID_JSON,
            str(exc),
        )

    events = tuple(normalizer.events)
    if parse_error is not None and parse_error.code not in {
        EvaluationErrorCode.NON_ZERO_EXIT
    }:
        return TraceParseResult(trace=None, events=events, error=parse_error)
    try:
        trace = _trace_from_events(
            events,
            request=request,
            trace_id=trace_id or f"trace-{run_id}-{case_id}-{iteration_id}",
            run_id=run_id,
            case_id=case_id,
            iteration_id=iteration_id,
            skill_version=skill_version,
            skill_sha256=skill_sha256,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        return TraceParseResult(
            trace=None,
            events=events,
            error=EvaluationError(EvaluationErrorCode.INVALID_TRACE, str(exc)),
        )
    return TraceParseResult(trace=trace, events=trace.events, error=parse_error)


def parse_stream_json_or_raise(
    source: RawSource | Iterable[EngineEvent],
    *,
    request: EngineRequest,
    run_id: str,
    case_id: str,
    iteration_id: str,
    trace_id: TraceId | None = None,
    process_exit_code: int | None = 0,
    skill_version: str | None = None,
    skill_sha256: str | None = None,
) -> Trace:
    """Convenience wrapper for callers that want an exception on bad input."""

    result = parse_stream_json(
        source,
        request=request,
        run_id=run_id,
        case_id=case_id,
        iteration_id=iteration_id,
        trace_id=trace_id,
        process_exit_code=process_exit_code,
        skill_version=skill_version,
        skill_sha256=skill_sha256,
    )
    return result.unwrap()


def trace_messages(trace: Trace) -> tuple[TraceMessage, ...]:
    """Group text chunks by message ID while retaining event identity and order."""

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
    ordered = sorted(grouped, key=lambda message_id: first_sequence[message_id])
    return tuple(
        TraceMessage(
            message_id=message_id,
            text="".join(item[2] for item in grouped[message_id]),
            event_ids=tuple(item[0] for item in grouped[message_id]),
            sequences=tuple(item[1] for item in grouped[message_id]),
        )
        for message_id in ordered
    )


def trace_tool_calls(trace: Trace) -> tuple[TraceToolCall, ...]:
    """Return tool calls joined to their observed results by tool_call_id."""

    results: dict[str, tuple[int, ToolResultPayload]] = {}
    for index, event in enumerate(trace.events):
        if isinstance(event.payload, ToolResultPayload):
            results.setdefault(event.payload.tool_call_id, (index, event.payload))
    calls: list[TraceToolCall] = []
    for index, event in enumerate(trace.events):
        payload = event.payload
        if not isinstance(payload, ToolCallPayload):
            continue
        result_info = results.get(payload.tool_call_id)
        calls.append(
            TraceToolCall(
                event_index=index,
                event_id=event.event_id,
                sequence=event.sequence,
                message_id=payload.message_id,
                tool_call_id=payload.tool_call_id,
                tool_name=payload.tool_name,
                arguments=payload.arguments,
                result=result_info[1] if result_info is not None else None,
                result_event_index=result_info[0] if result_info is not None else None,
            )
        )
    return tuple(calls)


def trace_usage(trace: Trace) -> Usage | None:
    """Return the canonical cumulative usage summary."""

    return trace.usage


def trace_exit_status(trace: Trace) -> EngineExitStatus:
    """Return the terminal engine outcome without conflating it with grading."""

    return trace.exit_status


@dataclass(frozen=True, slots=True)
class TraceParser:
    """Configured parser for one canonical Engine request."""

    request: EngineRequest
    run_id: str
    case_id: str
    iteration_id: str
    trace_id: TraceId | None = None
    skill_version: str | None = None
    skill_sha256: str | None = None

    def parse(
        self,
        source: RawSource | Iterable[EngineEvent],
        *,
        process_exit_code: int | None = 0,
    ) -> TraceParseResult:
        return parse_stream_json(
            source,
            request=self.request,
            run_id=self.run_id,
            case_id=self.case_id,
            iteration_id=self.iteration_id,
            trace_id=self.trace_id,
            process_exit_code=process_exit_code,
            skill_version=self.skill_version,
            skill_sha256=self.skill_sha256,
        )

    def parse_or_raise(
        self,
        source: RawSource | Iterable[EngineEvent],
        *,
        process_exit_code: int | None = 0,
    ) -> Trace:
        return self.parse(
            source,
            process_exit_code=process_exit_code,
        ).unwrap()


trace_from_events = build_trace
parse_trace = parse_stream_json_or_raise
