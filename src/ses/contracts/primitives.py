"""Scalar types shared by cross-module contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, TypeAlias

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    PlainSerializer,
    StrictInt,
    StrictStr,
)


class SchemaVersion(StrEnum):
    """Supported wire-schema versions."""

    V1ALPHA1 = "v1alpha1"


class RecordType(StrEnum):
    """Persistent record discriminators used by the Journey runtime."""

    ENGINE_REQUEST = "engine_request"
    ENGINE_EVENT = "engine_event"
    CASE_DEFINITION = "case_definition"
    SHOP_SNAPSHOT = "shop_snapshot"
    STATE_DIFF = "state_diff"
    TOOL_RESULT = "tool_result"
    TRACE = "trace"
    ASSERTION_RESULT = "assertion_result"
    CASE_GRADE = "case_grade"


def _validate_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be blank")
    return value


OpaqueId: TypeAlias = Annotated[StrictStr, AfterValidator(_validate_non_blank)]
NonEmptyStr: TypeAlias = Annotated[StrictStr, AfterValidator(_validate_non_blank)]
StrictNonNegativeInt: TypeAlias = Annotated[StrictInt, Field(ge=0)]
CurrencyCode: TypeAlias = Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
RunId: TypeAlias = OpaqueId
CaseId: TypeAlias = OpaqueId
IterationId: TypeAlias = OpaqueId
RequestId: TypeAlias = OpaqueId
EventId: TypeAlias = OpaqueId
MessageId: TypeAlias = OpaqueId
SessionId: TypeAlias = OpaqueId
TraceId: TypeAlias = OpaqueId
SnapshotId: TypeAlias = OpaqueId
DiffId: TypeAlias = OpaqueId
GradeId: TypeAlias = OpaqueId
AssertionId: TypeAlias = OpaqueId
ToolCallId: TypeAlias = OpaqueId


_RFC3339_DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _validate_datetime_input(value: object) -> object:
    if not isinstance(value, (str, datetime)):
        raise ValueError("UTC datetime must use an RFC 3339 string or datetime")
    if isinstance(value, str):
        if not _RFC3339_DATETIME_PATTERN.fullmatch(value):
            raise ValueError("UTC datetime string must use RFC 3339 with an offset")
        if value.endswith("-00:00"):
            raise ValueError("UTC datetime must use a known offset")
    return value


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a UTC offset")
    return value.astimezone(UTC)


def _serialize_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcDateTime: TypeAlias = Annotated[
    datetime,
    BeforeValidator(_validate_datetime_input),
    AfterValidator(_normalize_utc),
    PlainSerializer(_serialize_utc, return_type=str, when_used="json"),
]
