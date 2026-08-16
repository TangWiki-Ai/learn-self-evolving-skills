from __future__ import annotations

from ses.contracts import (
    CaseDefinition,
    CaseSplit,
    EngineRequest,
    RecordType,
    SchemaVersion,
)
from ses.evaluation import (
    EvaluationErrorCode,
    PreflightStatus,
    expect,
    run_after_expect,
)


def _case() -> CaseDefinition:
    return CaseDefinition(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.CASE_DEFINITION,
        case_id="case-1",
        source_id="state-bench-task-2",
        source_version="5644b183",
        transformation_version="return-v1",
        split=CaseSplit.DEVELOP,
        user_prompt="Return the defective order.",
        fixture_id="fixture-1",
        required_tools=("preview_return", "confirm_return"),
    )


def _request() -> EngineRequest:
    return EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id="request-1",
        prompt="Return the defective order.",
        allowed_tools=("preview_return", "confirm_return"),
        timeout_seconds=30,
    )


def _fixture() -> dict[str, object]:
    return {
        "fixture_id": "fixture-1",
        "available_tools": ("preview_return", "confirm_return"),
        "order_id": "order-1",
    }


def test_expect_passes_all_cheap_preconditions_without_an_engine() -> None:
    result = expect(
        _case(),
        _fixture(),
        request=_request(),
        budget={
            "max_total_tokens": 100,
            "max_cost_amount": "0.10",
            "cost_currency": "USD",
        },
        environment={"ready": True},
    )

    assert result.status is PreflightStatus.PASS
    assert result.failures == ()
    assert result.case == _case()


def test_expect_accumulates_case_fixture_tool_budget_and_environment_failures() -> None:
    result = expect(
        _case(),
        {"fixture_id": "other-fixture", "available_tools": ("preview_return",)},
        request=_request(),
        budget={"max_total_tokens": -1},
        environment={"ready": False},
    )

    codes = {failure.code for failure in result.failures}
    assert result.status is PreflightStatus.FAIL
    assert EvaluationErrorCode.MISSING_FIXTURE in codes
    assert EvaluationErrorCode.MISSING_TOOL in codes
    assert EvaluationErrorCode.INVALID_BUDGET in codes
    assert EvaluationErrorCode.ENVIRONMENT_NOT_READY in codes


def test_expect_rejects_an_invalid_case_before_tool_or_engine_checks() -> None:
    result = expect({"record_type": "case_definition"}, _fixture())

    assert result.status is PreflightStatus.FAIL
    assert [failure.code for failure in result.failures] == [
        EvaluationErrorCode.INVALID_CASE
    ]


def test_failed_expect_never_calls_engine() -> None:
    calls: list[EngineRequest] = []

    def engine(request: EngineRequest) -> object:
        calls.append(request)
        return "should not run"

    execution = run_after_expect(
        engine,
        _request(),
        _case(),
        _fixture(),
        available_tools=("preview_return",),
        budget={"max_total_tokens": 100},
        environment={"ready": True},
    )

    assert execution.engine_called is False
    assert execution.value is None
    assert execution.preflight.status is PreflightStatus.FAIL
    assert calls == []


def test_successful_expect_is_the_only_path_that_calls_engine() -> None:
    calls: list[EngineRequest] = []

    def engine(request: EngineRequest) -> str:
        calls.append(request)
        return "started"

    execution = run_after_expect(
        engine,
        _request(),
        _case(),
        _fixture(),
        budget={"max_total_tokens": 100},
        environment={"ready": True},
    )

    assert execution.engine_called is True
    assert execution.value == "started"
    assert calls == [_request()]
