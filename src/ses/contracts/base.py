"""Shared serialization primitives for cross-module contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, ClassVar, Literal, Never, Self, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    SerializerFunctionWrapHandler,
    StrictInt,
    StrictStr,
    model_serializer,
    model_validator,
)


class SchemaVersion(StrEnum):
    """Supported wire-schema versions."""

    V1ALPHA1 = "v1alpha1"


class RecordType(StrEnum):
    """Persistent record discriminators frozen for Issue #2."""

    ENGINE_REQUEST = "engine_request"
    ENGINE_EVENT = "engine_event"
    CASE_DEFINITION = "case_definition"
    SHOP_SNAPSHOT = "shop_snapshot"
    STATE_DIFF = "state_diff"
    TOOL_RESULT = "tool_result"
    TRACE = "trace"
    ASSERTION_RESULT = "assertion_result"
    CASE_GRADE = "case_grade"


class ArtifactRoot(StrEnum):
    """Roots from which artifact paths may be resolved."""

    WORKSPACE = "workspace"
    RUN = "run"


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

Sha256Digest: TypeAlias = Annotated[
    StrictStr,
    Field(pattern=r"^[0-9a-f]{64}$"),
]


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


_RFC3339_DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "authorization",
        "auth",
        "authentication",
        "bearer_token",
        "client_secret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "final_answer",
        "gold",
        "headers",
        "hidden_answer",
        "hidden_gold",
        "id_token",
        "password",
        "passwd",
        "private_answer",
        "private_key",
        "reference_answer",
        "reference_trace",
        "reference_trajectory",
        "refresh_token",
        "request_headers",
        "secret",
        "secret_key",
        "secrets",
        "selection_answer",
        "session_token",
        "set_cookie",
        "token",
        "x_api_key",
        "gold_answer",
    }
)
_ACRONYM_BOUNDARY_PATTERN = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_CASE_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_FIELD_SEPARATOR_PATTERN = re.compile(r"[^a-z0-9]+")


def _normalized_field_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.strip())
    with_acronym_boundaries = _ACRONYM_BOUNDARY_PATTERN.sub("_", normalized)
    with_word_boundaries = _CAMEL_CASE_PATTERN.sub("_", with_acronym_boundaries)
    return _FIELD_SEPARATOR_PATTERN.sub("_", with_word_boundaries.casefold()).strip("_")


def _is_forbidden_field_name(value: str) -> bool:
    normalized = _normalized_field_name(value)
    padded = f"_{normalized}_"
    return any(
        f"_{forbidden_phrase}_" in padded for forbidden_phrase in _FORBIDDEN_FIELD_NAMES
    )


def _validate_utf8(value: str, path: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"string at {path} must be valid UTF-8") from error


def _validate_public_data(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                _validate_utf8(key, f"{path}.<key>")
                if _is_forbidden_field_name(key):
                    raise ValueError(f"forbidden field {key!r} at {path}")
            child_path = f"{path}.{key}"
            _validate_public_data(child, child_path)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_public_data(child, f"{path}[{index}]")
    elif isinstance(value, str):
        _validate_utf8(value, path)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite float at {path} is not valid canonical JSON")


def _canonical_decimal(value: Decimal) -> str:
    return "0" if value == 0 else str(value)


def _canonical_value(value: object) -> JsonValue:
    if isinstance(value, ContractModel):
        return value._canonical_data()
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, datetime):
        return _serialize_utc(_normalize_utc(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("canonical JSON does not support non-finite decimals")
        return _canonical_decimal(value)
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON object keys must be strings")
            result[key] = _canonical_value(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: ContractModel | JsonValue) -> bytes:
    """Serialize a contract or JSON value into stable UTF-8 bytes."""
    canonical_value = _canonical_value(value)
    _validate_public_data(canonical_value)
    return json.dumps(
        canonical_value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: ContractModel | JsonValue) -> str:
    """Hash the stable canonical JSON representation with SHA-256."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _frozen_container_error() -> Never:
    raise TypeError("contract JSON containers are frozen")


class _FrozenDict(dict[str, object]):
    """A JSON object that preserves dict serialization without mutation."""

    def __setitem__(self, key: str, value: object) -> Never:
        _frozen_container_error()

    def __delitem__(self, key: str) -> Never:
        _frozen_container_error()

    def clear(self) -> Never:
        _frozen_container_error()

    def pop(self, key: str, default: object = None) -> Never:
        _frozen_container_error()

    def popitem(self) -> Never:
        _frozen_container_error()

    def setdefault(self, key: str, default: object = None) -> Never:
        _frozen_container_error()

    def update(self, *args: object, **kwargs: object) -> Never:
        _frozen_container_error()

    def __ior__(self, other: object) -> Never:  # type: ignore[misc]
        _frozen_container_error()

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenDict:
        memo[id(self)] = self
        return self


class _FrozenList(list[object]):
    """A JSON array that preserves list serialization without mutation."""

    def __setitem__(self, index: object, value: object) -> Never:
        _frozen_container_error()

    def __delitem__(self, index: object) -> Never:
        _frozen_container_error()

    def append(self, value: object) -> Never:
        _frozen_container_error()

    def clear(self) -> Never:
        _frozen_container_error()

    def extend(self, values: object) -> Never:
        _frozen_container_error()

    def insert(self, index: int, value: object) -> Never:  # type: ignore[override]
        _frozen_container_error()

    def pop(self, index: int = -1) -> Never:  # type: ignore[override]
        _frozen_container_error()

    def remove(self, value: object) -> Never:
        _frozen_container_error()

    def reverse(self) -> Never:
        _frozen_container_error()

    def sort(self, *args: object, **kwargs: object) -> Never:
        _frozen_container_error()

    def __iadd__(self, values: object) -> Never:  # type: ignore[misc]
        _frozen_container_error()

    def __imul__(self, count: int) -> Never:  # type: ignore[misc, override]
        _frozen_container_error()

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenList:
        memo[id(self)] = self
        return self


def _deep_freeze(value: object) -> object:
    if isinstance(value, ContractModel):
        return value
    if isinstance(value, Mapping):
        return _FrozenDict({key: _deep_freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return _FrozenList(_deep_freeze(child) for child in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(child) for child in value)
    return value


class ContractModel(BaseModel):
    """Strict, immutable base for all public contract values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        allow_inf_nan=False,
    )

    canonical_exclude: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _reject_sensitive_input(cls, value: object) -> object:
        _validate_public_data(value)
        return value

    @model_validator(mode="after")
    def _freeze_nested_values(self) -> ContractModel:
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            frozen = _deep_freeze(value)
            if frozen is not value:
                object.__setattr__(self, field_name, frozen)
        return self

    @model_serializer(mode="wrap")
    def _reject_sensitive_output(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> object:
        serialized = handler(self)
        _validate_public_data(serialized)
        return serialized

    def _canonical_data(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for field_name in type(self).model_fields:
            if field_name not in self.canonical_exclude:
                result[field_name] = _canonical_value(getattr(self, field_name))
        return result

    def canonical_json(self) -> bytes:
        """Return this model's stable hash projection as canonical JSON."""
        return canonical_json(self)

    def canonical_sha256(self) -> str:
        """Return the SHA-256 digest of this model's canonical JSON."""
        return canonical_sha256(self)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Copy this contract while revalidating every requested update."""
        if update:
            data = self.model_dump(mode="python", round_trip=True)
            data.update(update)
            return type(self).model_validate(data)
        return super().model_copy(deep=deep)


class VersionedRecord(ContractModel):
    """Base for persisted top-level records at the current schema version."""

    schema_version: Literal[SchemaVersion.V1ALPHA1] = SchemaVersion.V1ALPHA1

    @model_validator(mode="before")
    @classmethod
    def _reject_unsupported_version(cls, value: object) -> object:
        if isinstance(value, Mapping) and "schema_version" in value:
            supplied = value["schema_version"]
            if isinstance(supplied, SchemaVersion):
                supplied = supplied.value
            if supplied != SchemaVersion.V1ALPHA1.value:
                raise ValueError(
                    "unsupported schema_version "
                    f"{supplied!r}; expected {SchemaVersion.V1ALPHA1.value!r}"
                )
        return value


def _validate_artifact_path(value: str) -> str:
    if not value or value == ".":
        raise ValueError("artifact path must name a file")
    if "\x00" in value or "\\" in value:
        raise ValueError("artifact path must be a relative POSIX path")
    if value.endswith("/") or "//" in value:
        raise ValueError("artifact path must be canonical")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("artifact path must not use a Windows drive")
    path = PurePosixPath(value)
    if path.is_absolute() or value == "~" or value.startswith("~/"):
        raise ValueError("artifact path must be relative")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("artifact path must not traverse directories")
    if path.as_posix() != value:
        raise ValueError("artifact path must be canonical")
    return value


RelativeArtifactPath: TypeAlias = Annotated[
    StrictStr,
    AfterValidator(_validate_artifact_path),
]


def _validate_json_pointer(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError("JSON pointer must start with '/'")
    if re.search(r"~(?:[^01]|$)", value):
        raise ValueError("JSON pointer contains an invalid escape")
    return value


JsonPointer: TypeAlias = Annotated[
    StrictStr,
    AfterValidator(_validate_json_pointer),
]


class ArtifactRef(ContractModel):
    """Content-addressed file under a controlled workspace or run root."""

    root: ArtifactRoot
    path: RelativeArtifactPath
    sha256: Sha256Digest

    def verify_bytes(self, content: bytes) -> None:
        """Raise when content does not match the declared digest."""
        actual = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual, self.sha256):
            raise ValueError(
                f"artifact checksum mismatch: expected {self.sha256}, got {actual}"
            )
