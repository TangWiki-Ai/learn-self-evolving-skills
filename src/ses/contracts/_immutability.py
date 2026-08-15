"""Immutable container conversion for validated contract values."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import BaseModel


def deep_freeze(value: object) -> object:
    """Copy containers into structures without mutable base classes."""
    if isinstance(value, BaseModel):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: deep_freeze(child) for key, child in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(child) for child in value)
    return value


def deep_thaw(value: object, *, preserve_tuple: bool = False) -> object:
    """Return standard containers for validation and serialization."""
    if isinstance(value, BaseModel):
        return value
    if isinstance(value, Mapping):
        return {
            key: deep_thaw(child, preserve_tuple=False) for key, child in value.items()
        }
    if isinstance(value, tuple):
        values = [deep_thaw(child, preserve_tuple=False) for child in value]
        return tuple(values) if preserve_tuple else values
    if isinstance(value, list):
        return [deep_thaw(child, preserve_tuple=False) for child in value]
    return value
