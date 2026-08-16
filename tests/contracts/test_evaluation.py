from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    AssertionResult,
    CaseGrade,
    EngineEvent,
    EngineExitStatus,
    EngineRequest,
    EvidenceRef,
    GradeStatus,
    JudgeKind,
    RecordType,
    SchemaVersion,
    StateChange,
    StateDiff,
    Trace,
    Usage,
    artifact_json_bytes,
    content_sha256,
)

TRACE_SHA256 = "c7c5c1d70c5dec4416ab6158afd0b223ef40c29b1dc1f97ed9428b94d4cadb1c"
SKILL_SHA256 = "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"


def _request(
    *,
    request_id: str = "request-1",
    resume_session_id: str | None = None,
) -> EngineRequest:
    return EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id=request_id,
        prompt="Process the return request.",
        resume_session_id=resume_session_id,
        allowed_tools=("preview_return", "confirm_return"),
        timeout_seconds=30,
    )


def _event(
    *,
    event_id: str,
    sequence: int,
    request_id: str = "request-1",
    occurred_at: str = "2026-08-16T04:00:00Z",
    payload: dict[str, object] | None = None,
) -> EngineEvent:
    return EngineEvent.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "engine_event",
            "event_id": event_id,
            "request_id": request_id,
            "sequence": sequence,
            "occurred_at": occurred_at,
            "payload": payload
            or {
                "kind": "text_delta",
                "message_id": "message-1",
                "text": f"chunk-{sequence}",
            },
        }
    )


def _trace(
    *,
    request: EngineRequest | None = None,
    events: tuple[EngineEvent, ...] | None = None,
    usage: Usage | None = None,
    exit_status: EngineExitStatus = EngineExitStatus.SUCCESS,
    skill_version: str | None = "v0",
    skill_sha256: str | None = SKILL_SHA256,
) -> Trace:
    if events is None:
        events = (
            _event(event_id="event-1", sequence=0),
            _event(
                event_id="event-2",
                sequence=1,
                payload={
                    "kind": "usage",
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                },
            ),
            _event(
                event_id="event-3",
                sequence=2,
                payload={
                    "kind": "completed",
                    "exit_status": exit_status,
                    "session_id": "session-1",
                },
            ),
        )
    return Trace(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.TRACE,
        trace_id="trace-1",
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
        session_id="session-1",
        request=request or _request(),
        events=events,
        usage=usage or Usage(input_tokens=7, output_tokens=3),
        exit_status=exit_status,
        skill_version=skill_version,
        skill_sha256=skill_sha256,
    )


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        artifact=ArtifactRef(
            root=ArtifactRoot.RUN,
            path="traces/trace-1.json",
            sha256=TRACE_SHA256,
        ),
        json_pointer="/events/1/payload",
    )


def _assertion(*, assertion_id: str = "tool-order") -> AssertionResult:
    return AssertionResult(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ASSERTION_RESULT,
        assertion_id=assertion_id,
        judge=JudgeKind.RULE,
        judge_version="rule-v1",
        required=True,
        status=GradeStatus.PASS,
        reason="preview_return preceded confirm_return.",
        evidence=(_evidence(),),
    )


def test_trace_round_trips_with_request_events_usage_and_skill_identity() -> None:
    trace = _trace()

    restored = Trace.model_validate_json(trace.model_dump_json())

    assert restored == trace
    assert restored.record_type is RecordType.TRACE
    assert restored.exit_status is EngineExitStatus.SUCCESS


@pytest.mark.parametrize(
    "events",
    [
        (
            _event(event_id="event-1", sequence=1),
            _event(
                event_id="event-2",
                sequence=0,
                payload={"kind": "completed", "exit_status": "success"},
            ),
        ),
        (
            _event(event_id="event-1", sequence=0),
            _event(
                event_id="event-2",
                sequence=0,
                payload={"kind": "completed", "exit_status": "success"},
            ),
        ),
        (
            _event(event_id="event-1", sequence=0),
            _event(
                event_id="event-1",
                sequence=1,
                payload={"kind": "completed", "exit_status": "success"},
            ),
        ),
        (
            _event(event_id="event-1", sequence=0, request_id="missing"),
            _event(
                event_id="event-2",
                sequence=1,
                payload={"kind": "completed", "exit_status": "success"},
            ),
        ),
    ],
)
def test_trace_rejects_invalid_event_timelines(
    events: tuple[EngineEvent, ...],
) -> None:
    with pytest.raises(ValidationError):
        _trace(events=events)


@pytest.mark.parametrize(
    "events",
    [
        (_event(event_id="event-1", sequence=0),),
        (
            _event(
                event_id="event-1",
                sequence=0,
                payload={"kind": "completed", "exit_status": "success"},
            ),
            _event(event_id="event-2", sequence=1),
        ),
        (
            _event(
                event_id="event-1",
                sequence=0,
                payload={"kind": "completed", "exit_status": "success"},
            ),
            _event(
                event_id="event-2",
                sequence=1,
                payload={"kind": "completed", "exit_status": "success"},
            ),
        ),
        (
            _event(
                event_id="event-1",
                sequence=0,
                payload={"kind": "completed", "exit_status": "error"},
            ),
        ),
        (
            _event(
                event_id="event-1",
                sequence=0,
                payload={
                    "kind": "completed",
                    "exit_status": "success",
                    "session_id": "other-session",
                },
            ),
        ),
    ],
)
def test_trace_rejects_inconsistent_terminal_events(
    events: tuple[EngineEvent, ...],
) -> None:
    with pytest.raises(ValidationError):
        _trace(events=events)


def test_trace_requires_last_cumulative_usage_to_match_summary() -> None:
    events = (
        _event(
            event_id="event-1",
            sequence=0,
            payload={
                "kind": "usage",
                "usage": {"input_tokens": 8, "output_tokens": 3},
            },
        ),
        _event(
            event_id="event-2",
            sequence=1,
            payload={
                "kind": "completed",
                "exit_status": "success",
                "session_id": "session-1",
            },
        ),
    )

    with pytest.raises(ValidationError, match="cumulative usage"):
        _trace(events=events)


def test_trace_allows_missing_usage_when_request_ends_before_usage() -> None:
    events = (
        _event(
            event_id="event-1",
            sequence=0,
            payload={"kind": "error", "error_code": "stream", "message": "closed"},
        ),
        _event(
            event_id="event-2",
            sequence=1,
            payload={"kind": "completed", "exit_status": "error"},
        ),
    )
    trace = Trace(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.TRACE,
        trace_id="trace-1",
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
        request=_request(),
        events=events,
        usage=None,
        exit_status=EngineExitStatus.ERROR,
    )

    assert trace.usage is None


def test_success_trace_requires_a_session_id() -> None:
    completed = _event(
        event_id="event-1",
        sequence=0,
        payload={"kind": "completed", "exit_status": "success"},
    )

    with pytest.raises(ValidationError, match="successful Trace"):
        Trace(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.TRACE,
            trace_id="trace-1",
            run_id="run-1",
            case_id="case-1",
            iteration_id="iteration-0",
            request=_request(),
            events=(completed,),
            usage=None,
            exit_status=EngineExitStatus.SUCCESS,
        )


def test_resumed_trace_preserves_the_requested_session_id() -> None:
    with pytest.raises(ValidationError, match="resumed Trace"):
        _trace(request=_request(resume_session_id="session-original"))


@pytest.mark.parametrize(
    ("skill_version", "skill_sha256"),
    [("v0", None), (None, SKILL_SHA256)],
)
def test_trace_requires_complete_optional_skill_identity(
    skill_version: str | None,
    skill_sha256: str | None,
) -> None:
    with pytest.raises(ValidationError):
        _trace(skill_version=skill_version, skill_sha256=skill_sha256)


def test_trace_hash_excludes_nested_wall_clock_time() -> None:
    first = _trace()
    second = _trace(
        events=(
            _event(
                event_id="event-1",
                sequence=0,
                occurred_at="2026-08-17T04:00:00Z",
            ),
            _event(
                event_id="event-2",
                sequence=1,
                occurred_at="2026-08-17T04:00:01Z",
                payload={
                    "kind": "usage",
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                },
            ),
            _event(
                event_id="event-3",
                sequence=2,
                occurred_at="2026-08-17T04:00:02Z",
                payload={
                    "kind": "completed",
                    "exit_status": "success",
                    "session_id": "session-1",
                },
            ),
        )
    )

    assert content_sha256(first) == content_sha256(second)


def test_evidence_reference_round_trips_to_an_artifact_json_pointer() -> None:
    evidence = _evidence()

    assert EvidenceRef.model_validate_json(evidence.model_dump_json()) == evidence


def test_state_assertion_consumes_a_persisted_shop_diff_reference() -> None:
    diff = StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id="diff-1",
        before_snapshot_id="snapshot-before",
        after_snapshot_id="snapshot-after",
        changed={"/status": StateChange(before="shipped", after="returned")},
        summary="Order moved to returned.",
    )
    diff_bytes = artifact_json_bytes(diff)
    evidence = EvidenceRef(
        artifact=ArtifactRef(
            root=ArtifactRoot.RUN,
            path="diffs/diff-1.json",
            sha256=hashlib.sha256(diff_bytes).hexdigest(),
        ),
        json_pointer="/changed/~1status",
    )

    assertion = AssertionResult(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ASSERTION_RESULT,
        assertion_id="final-status",
        judge=JudgeKind.STATE,
        judge_version="state-v1",
        required=True,
        status=GradeStatus.PASS,
        reason="The final order status is returned.",
        evidence=(evidence,),
    )

    assertion.evidence[0].artifact.verify_bytes(diff_bytes)
    assert assertion.evidence[0].json_pointer == "/changed/~1status"


@pytest.mark.parametrize("json_pointer", ["", "events/0", "/events/~2"])
def test_evidence_reference_requires_a_valid_nonempty_json_pointer(
    json_pointer: str,
) -> None:
    with pytest.raises(ValidationError):
        EvidenceRef(
            artifact=_evidence().artifact,
            json_pointer=json_pointer,
        )


def test_assertion_result_round_trips_with_exact_grade_and_judge_enums() -> None:
    assertion = _assertion()

    restored = AssertionResult.model_validate_json(assertion.model_dump_json())

    assert restored == assertion
    assert restored.record_type is RecordType.ASSERTION_RESULT
    assert restored.status is GradeStatus.PASS
    assert restored.judge is JudgeKind.RULE


def test_model_judge_kinds_are_additive_contract_values() -> None:
    assert JudgeKind("llm") is JudgeKind.LLM
    assert JudgeKind("agent") is JudgeKind.AGENT


@pytest.mark.parametrize("status", ["passed", "FAIL", "budget_stop"])
def test_assertion_result_rejects_status_synonyms(status: str) -> None:
    data = _assertion().model_dump(mode="json")
    data["status"] = status

    with pytest.raises(ValidationError):
        AssertionResult.model_validate(data)


@pytest.mark.parametrize("status", [GradeStatus.PASS, GradeStatus.FAIL])
def test_decisive_assertions_require_evidence(status: GradeStatus) -> None:
    data = _assertion().model_dump()
    data["status"] = status
    data["evidence"] = ()

    with pytest.raises(ValidationError):
        AssertionResult.model_validate(data)


def test_not_evaluated_assertion_may_explain_missing_evidence() -> None:
    assertion = AssertionResult(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ASSERTION_RESULT,
        assertion_id="state-unavailable",
        judge=JudgeKind.STATE,
        judge_version="state-v1",
        required=True,
        status=GradeStatus.NOT_EVALUATED,
        reason="The final snapshot was unavailable.",
        evidence=(),
    )

    assert assertion.status is GradeStatus.NOT_EVALUATED


def test_case_grade_round_trips_without_embedding_gold() -> None:
    grade = CaseGrade(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.CASE_GRADE,
        grade_id="grade-1",
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
        status=GradeStatus.PASS,
        assertions=(_assertion(),),
    )

    restored = CaseGrade.model_validate_json(grade.model_dump_json())

    assert restored == grade
    assert restored.record_type is RecordType.CASE_GRADE


def test_case_grade_rejects_duplicate_assertion_ids() -> None:
    with pytest.raises(ValidationError):
        CaseGrade(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.CASE_GRADE,
            grade_id="grade-1",
            run_id="run-1",
            case_id="case-1",
            iteration_id="iteration-0",
            status=GradeStatus.FAIL,
            assertions=(_assertion(), _assertion()),
        )


def test_case_grade_allows_independent_judges_for_one_assertion() -> None:
    state_result = _assertion(assertion_id="business-outcome")
    rule_data = state_result.model_dump(mode="json")
    rule_data["judge"] = "state"
    state_result = AssertionResult.model_validate(rule_data)

    grade = CaseGrade(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.CASE_GRADE,
        grade_id="grade-1",
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
        status=GradeStatus.PASS,
        assertions=(_assertion(assertion_id="business-outcome"), state_result),
    )

    assert len(grade.assertions) == 2


@pytest.mark.parametrize(
    ("status", "assertion_status"),
    [
        (GradeStatus.PASS, GradeStatus.FAIL),
        (GradeStatus.FAIL, GradeStatus.PASS),
    ],
)
def test_case_grade_rejects_obviously_inconsistent_required_results(
    status: GradeStatus,
    assertion_status: GradeStatus,
) -> None:
    assertion_data = _assertion().model_dump(mode="json")
    assertion_data["status"] = assertion_status
    assertion = AssertionResult.model_validate(assertion_data)

    with pytest.raises(ValidationError, match="required assertion"):
        CaseGrade(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.CASE_GRADE,
            grade_id="grade-1",
            run_id="run-1",
            case_id="case-1",
            iteration_id="iteration-0",
            status=status,
            assertions=(assertion,),
        )


@pytest.mark.parametrize("status", [GradeStatus.PASS, GradeStatus.FAIL])
def test_decisive_case_grades_require_assertions(status: GradeStatus) -> None:
    with pytest.raises(ValidationError):
        CaseGrade(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.CASE_GRADE,
            grade_id="grade-1",
            run_id="run-1",
            case_id="case-1",
            iteration_id="iteration-0",
            status=status,
            assertions=(),
        )


def test_error_case_grade_may_exist_without_assertions() -> None:
    grade = CaseGrade(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.CASE_GRADE,
        grade_id="grade-1",
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
        status=GradeStatus.ERROR,
        assertions=(),
    )

    assert grade.status is GradeStatus.ERROR
