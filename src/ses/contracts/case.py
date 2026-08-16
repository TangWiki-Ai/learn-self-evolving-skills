"""Public executable-case definitions."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import field_validator

from ses.contracts.base import VersionedRecord
from ses.contracts.primitives import (
    CaseId,
    NonEmptyStr,
    RecordType,
)


class CaseSplit(StrEnum):
    """Persisted executable-case partitions used through Issue #7."""

    DEVELOP = "develop"
    SELECTION = "selection"
    FINAL = "final"


class CaseDefinition(VersionedRecord):
    """Public inputs needed to execute one isolated benchmark case."""

    record_type: Literal[RecordType.CASE_DEFINITION]
    case_id: CaseId
    source_id: NonEmptyStr
    source_version: NonEmptyStr
    transformation_version: NonEmptyStr
    split: CaseSplit
    user_prompt: NonEmptyStr
    fixture_id: NonEmptyStr
    required_tools: tuple[NonEmptyStr, ...] = ()

    @field_validator("required_tools")
    @classmethod
    def _reject_duplicate_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("required_tools must not contain duplicates")
        return value
