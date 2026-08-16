"""Errors returned by the deterministic evaluation boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EvaluationErrorCode(StrEnum):
    """Stable codes used by parser and preflight failures."""

    INVALID_JSON = "invalid_json"
    INVALID_EVENT = "invalid_event"
    MALFORMED_CRITICAL_EVENT = "malformed_critical_event"
    TRUNCATED_STREAM = "truncated_stream"
    TERMINAL_EVENT_NOT_LAST = "terminal_event_not_last"
    NON_ZERO_EXIT = "non_zero_exit"
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
    line_number: int | None = None
    event_type: str | None = None


class TraceParseError(ValueError):
    """Raised by the explicit raising parser helper."""

    def __init__(self, error: EvaluationError) -> None:
        self.error = error
        location = ""
        if error.line_number is not None:
            location = f" at line {error.line_number}"
        super().__init__(f"{error.code.value}{location}: {error.message}")


class PreflightError(ValueError):
    """Raised when a caller asks to run after a failed expect gate."""

    def __init__(self, errors: tuple[EvaluationError, ...]) -> None:
        self.errors = errors
        summary = "; ".join(error.message for error in errors)
        super().__init__(summary or "preflight failed")
