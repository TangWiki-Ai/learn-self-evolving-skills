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
    ValidationError,
    field_validator,
    model_validator,
)


class ConfigurationError(ValueError):
    """A configuration file is absent, malformed, or unsafe."""


class StrictModel(BaseModel):
    """Base for immutable configuration models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class ProviderId(StrEnum):
    """Supported live model providers."""

    SILICONFLOW = "siliconflow"
    CHATANYWHERE = "chatanywhere"


_PROVIDER_BASE_URLS: Mapping[ProviderId, frozenset[str]] = MappingProxyType(
    {
        ProviderId.SILICONFLOW: frozenset({"https://api.siliconflow.cn/"}),
        ProviderId.CHATANYWHERE: frozenset(
            {
                "https://api.chatanywhere.tech/",
                "https://api.chatanywhere.org/",
            }
        ),
    }
)


def _validate_https_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain credentials, query, or fragment")
    return value.rstrip("/") + "/"


def validate_provider_base_url(provider: ProviderId, base_url: str) -> str:
    """Return a normalized endpoint only when the provider owns it."""

    normalized = _validate_https_endpoint(base_url)
    if normalized not in _PROVIDER_BASE_URLS[provider]:
        raise ValueError(f"base_url is not allowed for provider {provider.value}")
    return normalized


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
    """Pinned model used by the live Agent execution path."""

    schema_version: StrictStr = Field(pattern=r"^v1alpha1$")
    engine: StrictStr = Field(pattern=r"^claude-code$")
    engine_version: StrictStr = Field(min_length=1)
    provider: ProviderId = ProviderId.SILICONFLOW
    model: LockedModel

    @field_validator("provider", mode="before")
    @classmethod
    def _parse_provider(cls, value: object) -> object:
        try:
            return ProviderId(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError("models lock contains an unknown provider") from exc

    @model_validator(mode="after")
    def _provider_owns_endpoint(self) -> ModelLock:
        try:
            validate_provider_base_url(self.provider, self.model.base_url)
        except ValueError as exc:
            raise ValueError(
                f"model base_url is not allowed for provider {self.provider.value}"
            ) from exc
        return self


class RuntimeConfig(StrictModel):
    """Project behavior configuration; credentials are deliberately absent."""

    schema_version: StrictStr = Field(pattern=r"^v1alpha1$")
    default_provider: ProviderId = ProviderId.SILICONFLOW
    models_lock: StrictStr = "models.lock.json"
    chatanywhere_models_lock: StrictStr = "models.chatanywhere.lock.json"
    data_manifest: StrictStr = "data/upstream/manifest.json"
    claude_executable: StrictStr = "claude"

    @field_validator("default_provider", mode="before")
    @classmethod
    def _parse_default_provider(cls, value: object) -> object:
        try:
            return ProviderId(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "runtime config contains an unknown default provider"
            ) from exc

    @field_validator("models_lock", "chatanywhere_models_lock", "data_manifest")
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

    def models_lock_for(self, provider: ProviderId) -> str:
        """Return the configured lock path for one provider."""

        if provider is ProviderId.CHATANYWHERE:
            return self.chatanywhere_models_lock
        return self.models_lock


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
    except ValidationError as exc:
        raise ConfigurationError(
            f"invalid runtime configuration {path}: {_validation_summary(exc)}"
        ) from exc


def load_model_lock(path: Path) -> ModelLock:
    """Load all concrete model identifiers from one immutable lock file."""
    try:
        return ModelLock.model_validate(_load_json(path))
    except ValidationError as exc:
        raise ConfigurationError(
            f"invalid models lock {path}: {_validation_summary(exc)}"
        ) from exc


def _validation_summary(error: ValidationError) -> str:
    """Format validation failures without Pydantic raw input values."""
    parts: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "$"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)
