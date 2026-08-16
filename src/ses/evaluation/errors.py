"""Errors returned by the deterministic evaluation boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EvaluationErrorCode(StrEnum):
    """Stable codes used by trace construction and preflight failures."""

    INVALID_TRACE = "invalid_trace"
    INVALID_CASE = "invalid_case"
    MISSING_FIXTURE = "missing_fixture"
    MISSING_TOOL = "missing_tool"
    INVALID_BUDGET = "invalid_budget"
    ENVIRONMENT_NOT_READY = "environment_not_ready"


@dataclass(frozen=True, slots=True)
class EvaluationError:
    """A safe, structured failure that does not include raw provider payloads."""

    code: EvaluationErrorCode
    message: str


class TraceBuildError(ValueError):
    """Raised when canonical events cannot form a valid Trace."""

    def __init__(self, error: EvaluationError) -> None:
        self.error = error
        super().__init__(f"{error.code.value}: {error.message}")


class PreflightError(ValueError):
    """Raised when a caller asks to run after a failed expect gate."""

    def __init__(self, errors: tuple[EvaluationError, ...]) -> None:
        self.errors = errors
        summary = "; ".join(error.message for error in errors)
        super().__init__(summary or "preflight failed")
