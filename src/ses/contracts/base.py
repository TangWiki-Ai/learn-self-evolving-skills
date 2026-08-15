"""Strict model lifecycle for cross-module contracts."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, ClassVar, Literal, Self, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    FieldSerializationInfo,
    SerializerFunctionWrapHandler,
    field_serializer,
    model_serializer,
    model_validator,
)

from ses.contracts._immutability import deep_freeze, deep_thaw
from ses.contracts.primitives import SchemaVersion
from ses.contracts.security import validate_public_data


class ContractModel(BaseModel):
    """Strict, deeply immutable base for all public contract values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )

    content_hash_exclude: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _reject_sensitive_input(cls, value: object) -> object:
        if isinstance(value, ContractModel):
            value = value.model_dump(mode="python", round_trip=True)
        else:
            value = deep_thaw(value)
        validate_public_data(value)
        return value

    @model_validator(mode="after")
    def _freeze_nested_values(self) -> ContractModel:
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            frozen = deep_freeze(value)
            if frozen is not value:
                object.__setattr__(self, field_name, frozen)
        return self

    @field_serializer("*", mode="wrap", check_fields=False)
    def _serialize_nested_values(
        self,
        value: object,
        handler: SerializerFunctionWrapHandler,
        info: FieldSerializationInfo,
    ) -> object:
        field = type(self).model_fields[info.field_name]
        return handler(
            deep_thaw(value, preserve_tuple=get_origin(field.annotation) is tuple)
        )

    @model_serializer(mode="wrap")
    def _reject_sensitive_output(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> object:
        serialized = handler(self)
        validate_public_data(serialized)
        return serialized

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return a revalidated copy; immutable values make `deep` equivalent."""
        del deep
        data = self.model_dump(mode="python", round_trip=True)
        if update:
            data.update(update)
        return type(self).model_validate(data)


class VersionedRecord(ContractModel):
    """Base for persisted top-level records at the current schema version."""

    schema_version: Literal[SchemaVersion.V1ALPHA1]

    @model_validator(mode="before")
    @classmethod
    def _validate_wire_header(cls, value: object) -> object:
        if isinstance(value, VersionedRecord):
            value = value.model_dump(mode="python", round_trip=True)
        if not isinstance(value, Mapping):
            return value
        if "schema_version" not in value:
            raise ValueError("missing schema_version in persisted record")
        supplied_version = value["schema_version"]
        if isinstance(supplied_version, Enum):
            supplied_version = supplied_version.value
        if supplied_version != SchemaVersion.V1ALPHA1.value:
            raise ValueError(
                "unsupported schema_version "
                f"{supplied_version!r}; expected {SchemaVersion.V1ALPHA1.value!r}"
            )

        if "record_type" not in value:
            raise ValueError("missing record_type in persisted record")
        record_field = cls.model_fields.get("record_type")
        if record_field is None:
            return value
        expected_values = get_args(record_field.annotation)
        if len(expected_values) != 1:
            return value
        expected = expected_values[0]
        if isinstance(expected, Enum):
            expected = expected.value
        supplied_record_type = value["record_type"]
        if isinstance(supplied_record_type, Enum):
            supplied_record_type = supplied_record_type.value
        if supplied_record_type != expected:
            raise ValueError(
                f"invalid record_type {supplied_record_type!r}; expected {expected!r}"
            )
        return value
