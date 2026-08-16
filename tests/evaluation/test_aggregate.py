from __future__ import annotations

import pytest

from ses.contracts import (
    AssertionResult,
    CaseGrade,
    GradeStatus,
)
from ses.evaluation import aggregate_case_grade, aggregate_status


def _assertion(
    status: GradeStatus, *, required: bool = True, index: int = 0
) -> AssertionResult:
    evidence: object = ()
    if status in {GradeStatus.PASS, GradeStatus.FAIL}:
        evidence = (
            {
                "artifact": {
                    "root": "run",
                    "path": f"assertions/{index}.json",
                    "sha256": "0" * 64,
                },
                "json_pointer": "/events",
            },
        )
    return AssertionResult.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "assertion_result",
            "assertion_id": f"assertion-{index}",
            "judge": "rule",
            "judge_version": "rule-v1",
            "required": required,
            "status": status,
            "reason": f"{status.value} for test",
            "evidence": evidence,
        }
    )


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([GradeStatus.PASS], GradeStatus.PASS),
        ([GradeStatus.FAIL], GradeStatus.FAIL),
        ([GradeStatus.NOT_EVALUATED], GradeStatus.NOT_EVALUATED),
        ([GradeStatus.ERROR], GradeStatus.ERROR),
        (
            [
                GradeStatus.PASS,
                GradeStatus.NOT_EVALUATED,
                GradeStatus.ERROR,
                GradeStatus.FAIL,
            ],
            GradeStatus.FAIL,
        ),
    ],
)
def test_failure_first_truth_table(
    statuses: list[GradeStatus], expected: GradeStatus
) -> None:
    assertions = tuple(
        _assertion(status, index=index) for index, status in enumerate(statuses)
    )

    assert aggregate_status(assertions) is expected


def test_infrastructure_error_is_not_reclassified_as_agent_fail() -> None:
    assertions = (_assertion(GradeStatus.FAIL),)

    assert aggregate_status(assertions, infrastructure_error=True) is GradeStatus.ERROR


def test_optional_failure_does_not_hide_a_required_pass() -> None:
    assertions = (
        _assertion(GradeStatus.PASS, index=1),
        _assertion(GradeStatus.FAIL, required=False, index=2),
    )

    assert aggregate_status(assertions) is GradeStatus.PASS


def test_case_grade_is_canonical_and_round_trips_with_independent_assertions() -> None:
    assertions = (
        _assertion(GradeStatus.PASS, index=1),
        _assertion(GradeStatus.PASS, index=2),
    )
    grade = aggregate_case_grade(
        assertions,
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
    )

    assert grade.status is GradeStatus.PASS
    assert CaseGrade.model_validate_json(grade.model_dump_json()) == grade
