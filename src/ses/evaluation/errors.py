"""Errors returned by the deterministic evaluation boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EvaluationErrorCode(StrEnum):
    """Stable code used by trace construction failures."""

    INVALID_TRACE = "invalid_trace"


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
