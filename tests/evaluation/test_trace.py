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
    Trace,
    UnknownPayload,
    Usage,
)
from ses.evaluation import (
    TraceBuildError,
    build_trace,
    trace_messages,
    trace_tool_calls,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "stream_json"


def _request() -> EngineRequest:
    return EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id="request-1",
        prompt="Return the order.",
        allowed_tools=("preview_return", "confirm_return"),
        timeout_seconds=30,
    )


def load_events(name: str) -> tuple[EngineEvent, ...]:
    """Load fixtures only through the canonical EngineEvent contract."""

    return tuple(
        EngineEvent.model_validate_json(line)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line
    )


def _trace(name: str) -> Trace:
    return build_trace(
        load_events(name),
        request=_request(),
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
    )


def test_canonical_fixture_preserves_events_messages_calls_usage_and_exit() -> None:
    trace = _trace("normal_flow.jsonl")

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


def test_unknown_canonical_event_is_retained_without_provider_parsing() -> None:
    unknown = _trace("unknown_event.jsonl").events[1].payload

    assert isinstance(unknown, UnknownPayload)
    assert unknown.source_type == "progress:heartbeat"


def test_parallel_calls_join_results_by_id_not_result_order() -> None:
    calls = trace_tool_calls(_trace("parallel_tools.jsonl"))

    assert [call.tool_call_id for call in calls] == ["tool-call-a", "tool-call-b"]
    assert [call.result_event_index for call in calls] == [3, 2]


def test_missing_terminal_event_cannot_form_a_trace() -> None:
    with pytest.raises(TraceBuildError, match="completed event"):
        _trace("truncated.jsonl")


def test_error_exit_is_projected_from_canonical_completed_event() -> None:
    trace = _trace("nonzero_exit.jsonl")

    assert trace.exit_status is EngineExitStatus.ERROR
    assert isinstance(trace.events[-1].payload, CompletedPayload)
    assert trace.events[-1].payload.exit_status is EngineExitStatus.ERROR


def test_build_trace_rejects_non_engine_event_inputs() -> None:
    with pytest.raises(TypeError, match="canonical EngineEvent"):
        build_trace(
            ({"payload": {"kind": "completed"}},),  # type: ignore[arg-type]
            request=_request(),
            run_id="run-1",
            case_id="case-1",
            iteration_id="iteration-0",
        )


def test_trace_round_trips_without_copying_the_contract() -> None:
    trace = _trace("normal_flow.jsonl")

    assert trace.events == load_events("normal_flow.jsonl")
    assert trace.model_validate_json(trace.model_dump_json()) == trace
