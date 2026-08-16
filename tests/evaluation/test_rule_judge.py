from __future__ import annotations

import hashlib
from pathlib import Path

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    EngineRequest,
    RecordType,
    SchemaVersion,
    Trace,
    artifact_json_bytes,
)
from ses.evaluation import (
    forbidden_call,
    judge_rules,
    parse_stream_json,
    tool_arguments,
    tool_called,
    tool_count,
    tool_order,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "stream_json" / "normal_flow.jsonl"


def _trace() -> Trace:
    request = EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id="request-1",
        prompt="Return the order.",
        allowed_tools=("preview_return", "confirm_return"),
        timeout_seconds=30,
    )
    result = parse_stream_json(
        FIXTURE.read_text(encoding="utf-8"),
        request=request,
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
    )
    assert result.trace is not None
    return result.trace


def _artifact(trace: Trace) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path="traces/trace-1.json",
        sha256=hashlib.sha256(artifact_json_bytes(trace)).hexdigest(),
    )


def test_rule_judge_supports_called_count_parameters_order_and_forbidden_call() -> None:
    trace = _trace()
    assertions = judge_rules(
        trace,
        (
            tool_called("preview_return"),
            tool_count("confirm_return", 1),
            tool_arguments(
                "confirm_return",
                {"order_id": "order-1", "amount_minor": 1299},
            ),
            tool_order(("preview_return", "confirm_return")),
            forbidden_call("cancel_return"),
        ),
        evidence_artifact=_artifact(trace),
    )

    assert [assertion.status.value for assertion in assertions] == [
        "pass",
        "pass",
        "pass",
        "pass",
        "pass",
    ]
    assert all(assertion.evidence for assertion in assertions)
    assert all(assertion.judge.value == "rule" for assertion in assertions)


def test_rule_judge_failure_evidence_points_to_the_offending_call() -> None:
    trace = _trace()
    assertions = judge_rules(
        trace,
        (
            tool_count("confirm_return", 2),
            tool_order(("confirm_return", "preview_return")),
            forbidden_call("preview_return"),
        ),
        evidence_artifact=_artifact(trace),
    )

    assert [assertion.status.value for assertion in assertions] == [
        "fail",
        "fail",
        "fail",
    ]
    assert all(assertion.evidence for assertion in assertions)
    assert any(
        evidence.json_pointer == "/events/5/payload"
        for evidence in assertions[0].evidence
    )


def test_rule_judge_requires_persisted_trace_evidence_for_decisions() -> None:
    assertions = judge_rules(_trace(), (tool_called("preview_return"),))

    assert assertions[0].status.value == "not_evaluated"
    assert assertions[0].evidence == ()
