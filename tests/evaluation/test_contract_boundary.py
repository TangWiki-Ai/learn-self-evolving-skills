from __future__ import annotations

import hashlib
from pathlib import Path

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    AssertionResult,
    CaseGrade,
    EngineEvent,
    EngineRequest,
    GradeStatus,
    RecordType,
    SchemaVersion,
    Trace,
    artifact_json_bytes,
)
from ses.evaluation import aggregate_case_grade, build_trace, judge_rules
from ses.evaluation.judges.rule import tool_called

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
    events = tuple(
        EngineEvent.model_validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
    )
    return build_trace(
        events,
        request=request,
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
    )


def _artifact(trace: Trace) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path="traces/trace-1.json",
        sha256=hashlib.sha256(artifact_json_bytes(trace)).hexdigest(),
    )


def test_rule_judge_results_are_consumable_by_case_grade_without_schema_copies() -> (
    None
):
    trace = _trace()
    assertions = judge_rules(
        trace,
        (tool_called("preview_return"),),
        evidence_artifact=_artifact(trace),
    )
    grade = aggregate_case_grade(
        assertions,
        run_id=trace.run_id,
        case_id=trace.case_id,
        iteration_id=trace.iteration_id,
    )

    restored_assertion = AssertionResult.model_validate_json(
        artifact_json_bytes(assertions[0])
    )
    restored_grade = CaseGrade.model_validate_json(artifact_json_bytes(grade))
    assert restored_assertion == assertions[0]
    assert restored_grade == grade
    assert restored_grade.status is GradeStatus.PASS


def test_evidence_digest_is_the_wire_digest_consumed_by_the_contract() -> None:
    trace = _trace()
    payload = artifact_json_bytes(trace)
    artifact = ArtifactRef(
        root=ArtifactRoot.RUN,
        path="traces/trace-1.json",
        sha256=hashlib.sha256(payload).hexdigest(),
    )

    artifact.verify_bytes(payload)
