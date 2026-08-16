from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ses.contracts import CaseDefinition, CaseSplit, RecordType, SchemaVersion, Usage
from ses.evaluation import (
    BudgetLimits,
    EvaluationErrorCode,
    ExpectResult,
    PreflightStatus,
    expect,
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


def _fixture() -> dict[str, object]:
    return {"fixture_id": "fixture-1", "order_id": "order-1"}


def _expect(**overrides: object) -> ExpectResult:
    values: dict[str, object] = {
        "case": _case(),
        "fixture": _fixture(),
        "fixture_id": "fixture-1",
        "available_tools": ("preview_return", "confirm_return"),
        "environment_ready": True,
        "environment_closed": False,
        "budget": {"max_total_tokens": 100},
    }
    values.update(overrides)
    return expect(**values)  # type: ignore[arg-type]


def test_expect_passes_explicit_preconditions_without_an_engine() -> None:
    result = _expect(
        budget={
            "max_total_tokens": 100,
            "max_cost_amount": "0.10",
            "cost_currency": "USD",
        }
    )

    assert result.status is PreflightStatus.PASS
    assert result.failures == ()
    assert result.case == _case()


def test_expect_accumulates_fixture_tool_budget_and_environment_failures() -> None:
    result = _expect(
        fixture_id="other-fixture",
        available_tools=("preview_return",),
        budget={"max_total_tokens": -1},
        environment_ready=False,
    )

    codes = {failure.code for failure in result.failures}
    assert result.status is PreflightStatus.FAIL
    assert codes == {
        EvaluationErrorCode.MISSING_FIXTURE,
        EvaluationErrorCode.MISSING_TOOL,
        EvaluationErrorCode.INVALID_BUDGET,
        EvaluationErrorCode.ENVIRONMENT_NOT_READY,
    }


def test_nonexistent_string_fixture_path_fails() -> None:
    result = _expect(fixture="does-not-exist.json")

    assert result.status is PreflightStatus.FAIL
    assert EvaluationErrorCode.MISSING_FIXTURE in {f.code for f in result.failures}


def test_expect_rejects_non_contract_case_input() -> None:
    result = _expect(case={"record_type": "case_definition"})

    assert EvaluationErrorCode.INVALID_CASE in {f.code for f in result.failures}


def test_existing_fixture_path_must_contain_matching_fixture_id(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    path.write_text('{"fixture_id":"fixture-1"}', encoding="utf-8")

    assert _expect(fixture=path).passed


@pytest.mark.parametrize("role", ["judge", "simulator"])
def test_toolless_environment_fails_for_every_execution_role(role: str) -> None:
    result = _expect(available_tools=())

    assert role  # Documents that neither caller role gets an implicit exception.
    assert EvaluationErrorCode.MISSING_TOOL in {f.code for f in result.failures}


def test_closed_environment_fails_even_when_marked_ready() -> None:
    result = _expect(environment_closed=True)

    assert EvaluationErrorCode.ENVIRONMENT_NOT_READY in {
        failure.code for failure in result.failures
    }


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: BudgetLimits(max_input_tokens=-1),
        lambda: BudgetLimits(max_output_tokens=-1),
        lambda: BudgetLimits(max_total_tokens=-1),
        lambda: BudgetLimits(max_cost_amount=Decimal("-0.01"), cost_currency="USD"),
        lambda: BudgetLimits.from_value(-1),
        lambda: BudgetLimits.from_value(
            {"max_cost_amount": "-0.01", "currency": "USD"}
        ),
    ],
)
def test_all_budget_entry_points_reject_negative_values(constructor: object) -> None:
    with pytest.raises(ValueError):
        constructor()  # type: ignore[operator]


def test_usage_budget_failure_is_reported_by_expect() -> None:
    result = _expect(
        budget=BudgetLimits(max_total_tokens=3),
        usage=Usage(input_tokens=2, output_tokens=2),
    )

    assert EvaluationErrorCode.INVALID_BUDGET in {f.code for f in result.failures}
