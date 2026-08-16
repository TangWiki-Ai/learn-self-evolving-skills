"""Environment-only credential loading and recursive redaction."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

SILICONFLOW_KEY_ENV = "SILICONFLOW_API_KEY"
_PROVIDER_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
        SILICONFLOW_KEY_ENV,
    }
)
_KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}")
_HEADER_PATTERN = re.compile(
    r"(?i)((?:authorization|proxy-authorization|x-api-key|"
    r"anthropic-api-key|api[_-]?key)\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_SENSITIVE_NAME_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|authorization|credential|password|secret|cookie|"
    r"(?:access|auth|bearer|id|refresh|session)[_-]?token)$"
)


class CredentialError(RuntimeError):
    """A required process credential is unavailable."""


@dataclass(frozen=True, repr=False)
class ProviderCredentials:
    """Secret material whose repr never exposes its value."""

    api_key: str

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise CredentialError(f"{SILICONFLOW_KEY_ENV} is empty")

    def __repr__(self) -> str:
        return "ProviderCredentials(api_key='[REDACTED]')"


def read_siliconflow_credentials(
    environ: Mapping[str, str],
) -> ProviderCredentials:
    """Read the only supported credential from the current process environment."""
    value = environ.get(SILICONFLOW_KEY_ENV, "").strip()
    if not value:
        raise CredentialError(
            f"missing {SILICONFLOW_KEY_ENV}; set it in the process environment"
        )
    return ProviderCredentials(api_key=value)


def build_claude_environment(
    source: Mapping[str, str],
    credentials: ProviderCredentials,
    *,
    base_url: str,
    model_id: str,
    config_dir: Path,
) -> dict[str, str]:
    """Build an isolated child environment without retaining global providers."""
    child = {
        key: value for key, value in source.items() if key not in _PROVIDER_ENV_NAMES
    }
    child.update(
        {
            "ANTHROPIC_API_KEY": credentials.api_key,
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": model_id,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model_id,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model_id,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model_id,
            "CLAUDE_CONFIG_DIR": str(config_dir),
        }
    )
    return child


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    """Redact known values, key-shaped values, and authentication headers."""
    result = text
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    result = _KEY_PATTERN.sub("[REDACTED]", result)
    return _HEADER_PATTERN.sub(r"\1[REDACTED]", result)


T = TypeVar("T")


def redact_data(value: T, secrets: Sequence[str] = ()) -> T:
    """Return a recursively redacted copy suitable for logs or diagnostics."""
    if isinstance(value, str):
        return redact(value, secrets)  # type: ignore[return-value]
    if isinstance(value, Mapping):
        cleaned: dict[object, object] = {}
        for key, child in value.items():
            if isinstance(key, str) and _SENSITIVE_NAME_PATTERN.search(key):
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = redact_data(child, secrets)
        return cleaned  # type: ignore[return-value]
    if isinstance(value, tuple):
        return tuple(redact_data(item, secrets) for item in value)  # type: ignore[return-value]
    if isinstance(value, list):
        return [redact_data(item, secrets) for item in value]  # type: ignore[return-value]
    return value
