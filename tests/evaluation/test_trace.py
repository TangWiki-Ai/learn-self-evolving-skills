from __future__ import annotations

from pathlib import Path

import pytest

from ses.contracts import (
    CompletedPayload,
    EngineEvent,
    EngineExitStatus,
    EngineRequest,
    RecordType,
    SchemaVersion,
    UnknownPayload,
    Usage,
)
from ses.evaluation import (
    EvaluationErrorCode,
    build_trace,
    parse_stream_json,
    trace_messages,
    trace_tool_calls,
)
from ses.evaluation.trace import TraceParseResult

FIXTURES = Path(__file__).parents[1] / "fixtures" / "stream_json"


def _request(*, resume_session_id: str | None = None) -> EngineRequest:
    return EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id="request-1",
        prompt="Return the order.",
        resume_session_id=resume_session_id,
        allowed_tools=("preview_return", "confirm_return"),
        timeout_seconds=30,
    )


def _parse(name: str, *, exit_code: int | None = 0) -> TraceParseResult:
    return parse_stream_json(
        (FIXTURES / name).read_text(encoding="utf-8"),
        request=_request(),
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
        process_exit_code=exit_code,
    )


def test_normal_fixture_preserves_timeline_messages_calls_results_usage_and_exit() -> (
    None
):
    result = _parse("normal_flow.jsonl")

    assert result.ok
    assert result.trace is not None
    trace = result.trace
    assert [event.sequence for event in trace.events] == list(range(9))
    assert len({event.event_id for event in trace.events}) == len(trace.events)
    assert trace.session_id == "session-normal"
    assert trace.exit_status is EngineExitStatus.SUCCESS
    assert trace.usage == Usage(input_tokens=17, output_tokens=11)
    assert (
        trace_messages(trace)[0].text
        == "I will check the return policy. Then I will confirm the return."
    )
    calls = trace_tool_calls(trace)
    assert [call.tool_name for call in calls] == ["preview_return", "confirm_return"]
    assert calls[0].arguments["order_id"] == "order-1"
    assert calls[0].result is not None
    assert calls[0].result_event_index == 4
    assert calls[1].result is not None
    assert isinstance(trace.events, tuple)
    with pytest.raises(TypeError):
        trace.events[0] = trace.events[0]  # type: ignore[index]


def test_unknown_noncritical_event_is_retained_as_canonical_unknown_payload() -> None:
    result = _parse("unknown_event.jsonl")

    assert result.ok
    assert result.trace is not None
    unknown = result.trace.events[1].payload
    assert isinstance(unknown, UnknownPayload)
    assert unknown.kind.value == "unknown"
    assert unknown.source_type == "progress:heartbeat"


def test_one_mapping_is_a_supported_stream_source() -> None:
    result = parse_stream_json(
        {
            "type": "result",
            "session_id": "session-one",
            "subtype": "success",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        request=_request(),
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
    )

    assert result.ok
    assert result.trace is not None
    assert result.trace.exit_status is EngineExitStatus.SUCCESS


def test_parallel_tool_blocks_keep_stable_call_ids_and_result_joining() -> None:
    result = _parse("parallel_tools.jsonl")

    assert result.ok
    assert result.trace is not None
    calls = trace_tool_calls(result.trace)
    assert [call.tool_call_id for call in calls] == ["tool-call-a", "tool-call-b"]
    assert [call.result_event_index for call in calls] == [4, 3]


def test_malformed_critical_event_returns_structured_failure_without_guessing() -> None:
    result = _parse("malformed_critical_event.jsonl")

    assert not result.ok
    assert result.trace is None
    assert result.error is not None
    assert result.error.code is EvaluationErrorCode.MALFORMED_CRITICAL_EVENT
    assert result.error.line_number == 2
    assert len(result.events) == 1


def test_truncated_fixture_does_not_become_a_partial_passing_trace() -> None:
    result = _parse("truncated.jsonl")

    assert not result.ok
    assert result.trace is None
    assert result.error is not None
    assert result.error.code is EvaluationErrorCode.TRUNCATED_STREAM


def test_nonzero_exit_keeps_error_exit_status_separate_from_parser_error() -> None:
    result = _parse("nonzero_exit.jsonl", exit_code=17)

    assert not result.ok
    assert result.trace is not None
    assert result.error is not None
    assert result.error.code is EvaluationErrorCode.NON_ZERO_EXIT
    assert result.trace.exit_status is EngineExitStatus.ERROR
    assert isinstance(result.trace.events[-1].payload, CompletedPayload)
    assert result.trace.events[-1].payload.exit_status is EngineExitStatus.ERROR


def test_normalized_engine_events_are_consumed_without_copying_the_trace_contract() -> (
    None
):
    request = _request()
    events = (
        request_event(
            "event-text",
            10,
            {"kind": "text_delta", "message_id": "message-1", "text": "done"},
        ),
        request_event(
            "event-usage",
            11,
            {"kind": "usage", "usage": {"input_tokens": 1, "output_tokens": 2}},
        ),
        request_event(
            "event-completed",
            12,
            {
                "kind": "completed",
                "exit_status": "success",
                "session_id": "session-1",
            },
        ),
    )

    trace = build_trace(
        events,
        request=request,
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
    )

    assert trace.events == events
    assert [event.sequence for event in trace.events] == [10, 11, 12]
    assert trace.model_validate_json(trace.model_dump_json()) == trace


def request_event(
    event_id: str, sequence: int, payload: dict[str, object]
) -> EngineEvent:
    return EngineEvent.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "engine_event",
            "event_id": event_id,
            "request_id": "request-1",
            "sequence": sequence,
            "occurred_at": "2026-08-16T04:00:00Z",
            "payload": payload,
        }
    )
