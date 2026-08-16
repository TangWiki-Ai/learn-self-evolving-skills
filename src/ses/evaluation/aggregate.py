"""Failure-first aggregation for deterministic Judge results."""

from __future__ import annotations

from collections.abc import Iterable

from ses.contracts import (
    AssertionResult,
    CaseGrade,
    GradeStatus,
    RecordType,
    SchemaVersion,
)


def aggregate_status(
    assertions: Iterable[AssertionResult],
    *,
    infrastructure_error: bool = False,
) -> GradeStatus:
    """Apply the failure-first truth table to required assertions.

    A preflight/parser/infrastructure error takes the ``error`` branch.  Once
    that branch is absent, a required ``fail`` takes precedence over required
    ``error`` and ``not_evaluated`` so a real failed assertion cannot be hidden
    by a later judge problem.
    """

    items = tuple(assertions)
    if infrastructure_error:
        return GradeStatus.ERROR
    required = tuple(item for item in items if item.required)
    if any(item.status is GradeStatus.FAIL for item in required):
        return GradeStatus.FAIL
    if any(item.status is GradeStatus.ERROR for item in required):
        return GradeStatus.ERROR
    if any(item.status is GradeStatus.NOT_EVALUATED for item in required):
        return GradeStatus.NOT_EVALUATED
    if not required:
        if not items:
            return GradeStatus.NOT_EVALUATED
        if any(item.status is GradeStatus.FAIL for item in items):
            return GradeStatus.NOT_EVALUATED
        if any(item.status is GradeStatus.ERROR for item in items):
            return GradeStatus.ERROR
        if any(item.status is GradeStatus.NOT_EVALUATED for item in items):
            return GradeStatus.NOT_EVALUATED
    return GradeStatus.PASS


def aggregate_case_grade(
    assertions: Iterable[AssertionResult],
    *,
    run_id: str,
    case_id: str,
    iteration_id: str,
    grade_id: str | None = None,
    infrastructure_error: bool = False,
) -> CaseGrade:
    """Create the canonical CaseGrade without rewriting assertion records."""

    items = tuple(assertions)
    return CaseGrade(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.CASE_GRADE,
        grade_id=grade_id or f"grade-{run_id}-{case_id}-{iteration_id}",
        run_id=run_id,
        case_id=case_id,
        iteration_id=iteration_id,
        status=aggregate_status(items, infrastructure_error=infrastructure_error),
        assertions=items,
    )


aggregate = aggregate_status
