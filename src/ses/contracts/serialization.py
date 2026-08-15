"""Stable artifact serialization and semantic content hashing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, JsonValue

from ses.contracts.artifact import Sha256Digest
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.primitives import _serialize_utc
from ses.contracts.security import validate_public_data


def _decimal_string(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("stable JSON does not support non-finite decimals")
    return "0" if value == 0 else str(value)


def _stable_value(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return {
            field_name: _stable_value(getattr(value, field_name))
            for field_name in type(value).model_fields
        }
    if isinstance(value, Enum):
        return _stable_value(value.value)
    if isinstance(value, datetime):
        return _serialize_utc(value)
    if isinstance(value, Decimal):
        return _decimal_string(value)
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("stable JSON object keys must be strings")
            result[key] = _stable_value(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_stable_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported stable JSON value: {type(value).__name__}")


def _stable_json_bytes(value: object) -> bytes:
    stable_value = _stable_value(value)
    validate_public_data(stable_value)
    return json.dumps(
        stable_value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _content_projection(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        excluded: frozenset[str] = getattr(
            type(value), "content_hash_exclude", frozenset()
        )
        return {
            field_name: _content_projection(getattr(value, field_name))
            for field_name in type(value).model_fields
            if field_name not in excluded
        }
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("content projection object keys must be strings")
            result[key] = _content_projection(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_content_projection(child) for child in value]
    return _stable_value(value)


def artifact_json_bytes(record: VersionedRecord) -> bytes:
    """Serialize a complete persisted record to stable canonical wire bytes."""
    if not isinstance(record, VersionedRecord):
        raise TypeError("artifact_json_bytes requires a VersionedRecord")
    record = type(record).model_validate(record)
    wire = record.model_dump(mode="json", round_trip=True)
    return _stable_json_bytes(wire)


def content_sha256(record: ContractModel) -> Sha256Digest:
    """Hash a contract's stable semantic projection with SHA-256."""
    if not isinstance(record, ContractModel):
        raise TypeError("content_sha256 requires a ContractModel")
    record = type(record).model_validate(record)
    digest = hashlib.sha256(_stable_json_bytes(_content_projection(record))).hexdigest()
    return digest
