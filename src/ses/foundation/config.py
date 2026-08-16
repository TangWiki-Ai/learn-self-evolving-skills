"""Strict, credential-free runtime configuration and model locks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_serializer,
    field_validator,
)


class ConfigurationError(ValueError):
    """A configuration file is absent, malformed, or unsafe."""


class StrictModel(BaseModel):
    """Base for immutable configuration models."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ModelRole(StrEnum):
    """Course roles resolved through the checked model lock."""

    MAIN = "main"
    CREATOR = "creator"
    SIMULATOR = "simulator"
    JUDGE = "judge"


def _validate_https_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain credentials, query, or fragment")
    return value.rstrip("/") + "/"


class LockedModel(StrictModel):
    """One provider-neutral role binding used by the Claude adapter."""

    model_id: StrictStr = Field(min_length=1)
    base_url: StrictStr

    _endpoint = field_validator("base_url")(_validate_https_endpoint)

    @field_validator("model_id")
    @classmethod
    def _model_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_id must not be blank")
        return value


class ModelLock(StrictModel):
    """Pinned model identifiers for every course role."""

    schema_version: StrictStr = Field(pattern=r"^v1alpha1$")
    engine: StrictStr = Field(pattern=r"^claude-code$")
    engine_version: StrictStr = Field(min_length=1)
    roles: Mapping[ModelRole, LockedModel]

    @field_validator("roles", mode="before")
    @classmethod
    def _parse_role_keys(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        try:
            return {ModelRole(key): model for key, model in value.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError("models lock contains an unknown role") from exc

    @field_validator("roles")
    @classmethod
    def _all_roles_are_locked(
        cls, value: Mapping[ModelRole, LockedModel]
    ) -> Mapping[ModelRole, LockedModel]:
        missing = set(ModelRole) - set(value)
        if missing:
            names = ", ".join(sorted(role.value for role in missing))
            raise ValueError(f"models lock is missing roles: {names}")
        return MappingProxyType(dict(value))

    @field_serializer("roles")
    def _serialize_roles(
        self, value: Mapping[ModelRole, LockedModel]
    ) -> dict[str, LockedModel]:
        return {role.value: model for role, model in value.items()}


class RuntimeConfig(StrictModel):
    """Project behavior configuration; credentials are deliberately absent."""

    schema_version: StrictStr = Field(pattern=r"^v1alpha1$")
    models_lock: StrictStr = "models.lock.json"
    data_manifest: StrictStr = "data/upstream/manifest.json"
    workspace_root: StrictStr = ".ses/workspaces"
    claude_executable: StrictStr = "claude"

    @field_validator("models_lock", "data_manifest", "workspace_root")
    @classmethod
    def _relative_project_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not value.strip():
            raise ValueError("project paths must be non-empty relative paths")
        return path.as_posix()

    @field_validator("claude_executable")
    @classmethod
    def _executable_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("claude_executable must not be blank")
        return value


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file does not exist: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"cannot read configuration file: {path}: {exc}"
        ) from exc


def load_runtime_config(path: Path) -> RuntimeConfig:
    """Load a runtime file with strict schema and unknown-field rejection."""
    try:
        return RuntimeConfig.model_validate(_load_json(path))
    except ValueError as exc:
        raise ConfigurationError(
            f"invalid runtime configuration {path}: {exc}"
        ) from exc


def load_model_lock(path: Path) -> ModelLock:
    """Load all concrete model identifiers from one immutable lock file."""
    try:
        return ModelLock.model_validate(_load_json(path))
    except ValueError as exc:
        raise ConfigurationError(f"invalid models lock {path}: {exc}") from exc
