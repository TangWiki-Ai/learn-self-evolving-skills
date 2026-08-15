from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ses.contracts import (
    EngineEvent,
    EngineEventKind,
    EngineExitStatus,
    EngineRequest,
    RecordType,
    Usage,
)


def _event(payload: dict[str, object], *, sequence: int = 0) -> EngineEvent:
    return EngineEvent.model_validate(
        {
            "event_id": f"event-{sequence}",
            "request_id": "request-1",
            "sequence": sequence,
            "occurred_at": "2026-08-16T12:00:00+08:00",
            "payload": payload,
        }
    )


def test_engine_request_round_trips_without_provider_configuration() -> None:
    request = EngineRequest(
        request_id="request-1",
        prompt="Process the return request.",
        resume_session_id=None,
        allowed_tools=("preview_return", "confirm_return"),
        timeout_seconds=30,
    )

    restored = EngineRequest.model_validate_json(request.model_dump_json())

    assert restored == request
    assert restored.schema_version.value == "v1alpha1"
    assert restored.record_type is RecordType.ENGINE_REQUEST


@pytest.mark.parametrize(
    "allowed_tools",
    [("preview_return", "preview_return"), ("",), ("   ",)],
)
def test_engine_request_rejects_invalid_tool_allowlists(
    allowed_tools: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        EngineRequest(
            request_id="request-1",
            prompt="Process the return request.",
            allowed_tools=allowed_tools,
            timeout_seconds=30,
        )


@pytest.mark.parametrize("timeout_seconds", [True, "30", 0, -1])
def test_engine_request_requires_a_strict_positive_timeout(
    timeout_seconds: object,
) -> None:
    with pytest.raises(ValidationError):
        EngineRequest.model_validate(
            {
                "request_id": "request-1",
                "prompt": "Process the return request.",
                "timeout_seconds": timeout_seconds,
            }
        )


@pytest.mark.parametrize(
    "field",
    ["api_key", "headers", "provider", "environment"],
)
def test_engine_request_rejects_provider_private_fields(field: str) -> None:
    data: dict[str, object] = {
        "request_id": "request-1",
        "prompt": "Process the return request.",
        "timeout_seconds": 30,
        field: "not-public",
    }

    with pytest.raises(ValidationError):
        EngineRequest.model_validate(data)


def test_usage_preserves_sub_minor_cost_as_a_decimal_string() -> None:
    usage = Usage(
        input_tokens=7,
        output_tokens=3,
        cost_amount=Decimal("0.001230"),
        cost_currency="USD",
    )

    wire = json.loads(usage.model_dump_json())

    assert wire["cost_amount"] == "0.001230"
    assert Usage.model_validate_json(usage.model_dump_json()) == usage


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_tokens", -1),
        ("input_tokens", True),
        ("input_tokens", 1.5),
        ("input_tokens", "1"),
        ("output_tokens", -1),
        ("output_tokens", False),
    ],
)
def test_usage_requires_strict_nonnegative_token_counts(
    field: str,
    value: object,
) -> None:
    data: dict[str, object] = {"input_tokens": 1, "output_tokens": 1}
    data[field] = value

    with pytest.raises(ValidationError):
        Usage.model_validate(data)


@pytest.mark.parametrize("cost_amount", [0.01, 1, "NaN", "Infinity", "-0.01"])
def test_usage_rejects_noncanonical_cost_values(cost_amount: object) -> None:
    with pytest.raises(ValidationError):
        Usage.model_validate(
            {
                "input_tokens": 1,
                "output_tokens": 1,
                "cost_amount": cost_amount,
                "cost_currency": "USD",
            }
        )


@pytest.mark.parametrize(
    "data",
    [
        {"input_tokens": 1, "output_tokens": 1, "cost_amount": "0.01"},
        {"input_tokens": 1, "output_tokens": 1, "cost_currency": "USD"},
        {
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_amount": "0.01",
            "cost_currency": "usd",
        },
    ],
)
def test_usage_requires_a_complete_canonical_cost_pair(
    data: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Usage.model_validate(data)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "text_delta",
            "message_id": "message-1",
            "text": "Refund started.",
        },
        {
            "kind": "tool_call",
            "message_id": "message-1",
            "tool_call_id": "tool-1",
            "tool_name": "preview_return",
            "arguments": {"order_id": "order-1"},
        },
        {
            "kind": "tool_result",
            "tool_call_id": "tool-1",
            "content": {"status": "success"},
            "is_error": False,
        },
        {
            "kind": "usage",
            "usage": {"input_tokens": 7, "output_tokens": 3},
        },
        {"kind": "error", "error_code": "process_exit", "message": "failed"},
        {
            "kind": "completed",
            "exit_status": "success",
            "session_id": "session-1",
        },
        {"kind": "unknown", "source_type": "provider_heartbeat"},
    ],
)
def test_engine_event_variants_round_trip(payload: dict[str, object]) -> None:
    event = _event(payload)

    restored = EngineEvent.model_validate_json(event.model_dump_json())

    assert restored == event
    assert restored.record_type is RecordType.ENGINE_EVENT
    assert restored.occurred_at == datetime(2026, 8, 16, 4, 0, tzinfo=UTC)


def test_engine_event_uses_exact_enums() -> None:
    completed = _event(
        {
            "kind": EngineEventKind.COMPLETED,
            "exit_status": EngineExitStatus.BUDGET_STOP,
        }
    )

    assert completed.payload.kind is EngineEventKind.COMPLETED

    with pytest.raises(ValidationError):
        _event({"kind": "finished", "exit_status": "success"})


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "text_delta"},
        {
            "kind": "tool_call",
            "message_id": "message-1",
            "tool_call_id": "tool-1",
        },
        {"kind": "usage", "usage": {"input_tokens": 1}},
        {"kind": "completed", "exit_status": "passed"},
        {
            "kind": "unknown",
            "source_type": "heartbeat",
            "provider_payload": {"private": True},
        },
    ],
)
def test_engine_event_rejects_malformed_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _event(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "tool_call",
            "message_id": "message-1",
            "tool_call_id": "tool-1",
            "tool_name": "preview_return",
            "arguments": {"nested": [{"api/key": "not-a-real-value"}]},
        },
        {
            "kind": "tool_result",
            "tool_call_id": "tool-1",
            "content": {"responseHeaders": {"x-api-key": "not-a-real-value"}},
            "is_error": False,
        },
    ],
)
def test_engine_json_payloads_reject_nested_credentials(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="forbidden field"):
        _event(payload)


def test_engine_event_requires_a_nonnegative_strict_sequence() -> None:
    with pytest.raises(ValidationError):
        _event(
            {"kind": "text_delta", "message_id": "message-1", "text": "chunk"},
            sequence=-1,
        )

    data = _event(
        {"kind": "text_delta", "message_id": "message-1", "text": "chunk"}
    ).model_dump()
    data["sequence"] = True
    with pytest.raises(ValidationError):
        EngineEvent.model_validate(data)


def test_engine_event_hash_excludes_wall_clock_time() -> None:
    first = _event({"kind": "text_delta", "message_id": "message-1", "text": "same"})
    second_data = first.model_dump(mode="json")
    second_data["occurred_at"] = "2026-08-17T04:00:00Z"
    second = EngineEvent.model_validate(second_data)

    assert first.canonical_sha256() == second.canonical_sha256()
