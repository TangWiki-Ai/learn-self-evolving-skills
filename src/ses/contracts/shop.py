"""Deterministic shop-state and tool-result contracts."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import Field, JsonValue, StrictInt, field_validator, model_validator

from ses.contracts.artifact import JsonPointer
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.primitives import (
    CaseId,
    CurrencyCode,
    DiffId,
    NonEmptyStr,
    RecordType,
    SnapshotId,
    UtcDateTime,
)
from ses.contracts.serialization import _stable_json_bytes


def _reject_binary_floats(value: object, path: str = "$") -> object:
    if isinstance(value, float):
        raise ValueError(
            f"binary float at {path} is not allowed in canonical shop data"
        )
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_binary_floats(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_binary_floats(child, f"{path}[{index}]")
    return value


class ToolResultStatus(StrEnum):
    """Whether a shop tool applied its operation."""

    SUCCESS = "success"
    ERROR = "error"


class Money(ContractModel):
    """Business money represented only in integer minor units."""

    amount_minor: StrictInt
    currency: CurrencyCode


class ShopSnapshot(VersionedRecord):
    """A deterministic view of shop state at one point in a case."""

    content_hash_exclude = frozenset({"captured_at"})

    record_type: Literal[RecordType.SHOP_SNAPSHOT]
    snapshot_id: SnapshotId
    case_id: CaseId
    captured_at: UtcDateTime
    policy_version: NonEmptyStr
    state: Mapping[str, JsonValue]

    @field_validator("state", mode="before")
    @classmethod
    def _require_exact_state_numbers(cls, value: object) -> object:
        return _reject_binary_floats(value)


class StateChange(ContractModel):
    """The before and after values at one changed JSON pointer."""

    before: JsonValue
    after: JsonValue

    @field_validator("before", "after", mode="before")
    @classmethod
    def _require_exact_values(cls, value: object) -> object:
        return _reject_binary_floats(value)

    @model_validator(mode="after")
    def _require_a_real_change(self) -> StateChange:
        if _stable_json_bytes(self.before) == _stable_json_bytes(self.after):
            raise ValueError("state change before and after values must differ")
        return self


class StateDiff(VersionedRecord):
    """Business-meaningful changes between two shop snapshots."""

    content_hash_exclude = frozenset({"summary"})

    record_type: Literal[RecordType.STATE_DIFF]
    diff_id: DiffId
    before_snapshot_id: SnapshotId
    after_snapshot_id: SnapshotId
    added: Mapping[JsonPointer, JsonValue] = Field(default_factory=dict)
    removed: Mapping[JsonPointer, JsonValue] = Field(default_factory=dict)
    changed: Mapping[JsonPointer, StateChange] = Field(default_factory=dict)
    summary: str = ""

    @field_validator("added", "removed", mode="before")
    @classmethod
    def _require_exact_diff_numbers(cls, value: object) -> object:
        return _reject_binary_floats(value)

    @model_validator(mode="after")
    def _validate_snapshot_and_path_identity(self) -> StateDiff:
        added_paths = set(self.added)
        removed_paths = set(self.removed)
        changed_paths = set(self.changed)
        if self.before_snapshot_id == self.after_snapshot_id and (
            added_paths or removed_paths or changed_paths
        ):
            raise ValueError("changed state requires two distinct snapshots")
        if (
            added_paths & removed_paths
            or added_paths & changed_paths
            or removed_paths & changed_paths
        ):
            raise ValueError("StateDiff path buckets must be disjoint")
        return self


class ToolResult(VersionedRecord):
    """The atomic outcome of one shop tool call."""

    record_type: Literal[RecordType.TOOL_RESULT]
    tool_name: NonEmptyStr
    status: ToolResultStatus
    data: JsonValue = None
    error_code: NonEmptyStr | None = None
    error_message: NonEmptyStr | None = None

    @field_validator("data", mode="before")
    @classmethod
    def _require_exact_result_numbers(cls, value: object) -> object:
        return _reject_binary_floats(value)

    @model_validator(mode="after")
    def _validate_status_shape(self) -> ToolResult:
        if self.status is ToolResultStatus.SUCCESS:
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("successful tool results must not contain errors")
        elif self.error_code is None or self.error_message is None:
            raise ValueError("failed tool results require an error code and message")
        return self
