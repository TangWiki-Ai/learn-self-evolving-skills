"""Environment-only credential loading and recursive redaction."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote, urlsplit

from ses.foundation.config import ProviderId, validate_provider_base_url

SILICONFLOW_KEY_ENV = "SILICONFLOW_API_KEY"
CHATANYWHERE_KEY_ENV = "CHATANYWHERE_API_KEY"
_PROVIDER_KEY_ENV = {
    ProviderId.SILICONFLOW: SILICONFLOW_KEY_ENV,
    ProviderId.CHATANYWHERE: CHATANYWHERE_KEY_ENV,
}
_CLAUDE_AUTH_ENV = {
    ProviderId.SILICONFLOW: "ANTHROPIC_API_KEY",
    ProviderId.CHATANYWHERE: "ANTHROPIC_AUTH_TOKEN",
}
_PROXY_URL_ENV_NAMES = frozenset({"ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"})
_PROXY_ENV_NAMES = _PROXY_URL_ENV_NAMES | {"NO_PROXY"}
_SAFE_ENV_NAMES = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NODE_EXTRA_CA_CERTS",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "TZ",
    }
    | _PROXY_ENV_NAMES
    | {name.casefold() for name in _PROXY_ENV_NAMES}
)
_KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}")
_HEADER_PATTERN = re.compile(
    r"(?i)((?:authorization|proxy-authorization|x-api-key|"
    r"anthropic-api-key|api[_-]?key)\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_SENSITIVE_NAME_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|authorization|auth|credential|credentials|password|"
    r"passwd|secret|secrets|cookie|cookies|token|tokens|private_?key)($|_)"
)


class CredentialError(RuntimeError):
    """A required process credential is unavailable."""


@dataclass(frozen=True, repr=False)
class ProviderCredentials:
    """Secret material whose repr never exposes its value."""

    api_key: str
    provider: ProviderId = ProviderId.SILICONFLOW

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise CredentialError(f"{_PROVIDER_KEY_ENV[self.provider]} is empty")

    def __repr__(self) -> str:
        return (
            f"ProviderCredentials(provider={self.provider.value!r}, "
            "api_key='[REDACTED]')"
        )


def read_provider_credentials(
    provider: ProviderId,
    environ: Mapping[str, str],
) -> ProviderCredentials:
    """Read one provider's credential from the process environment."""

    name = _PROVIDER_KEY_ENV[provider]
    value = environ.get(name, "").strip()
    if not value:
        raise CredentialError(f"missing {name}; set it in the process environment")
    return ProviderCredentials(api_key=value, provider=provider)


def read_siliconflow_credentials(
    environ: Mapping[str, str],
) -> ProviderCredentials:
    """Read the SiliconFlow credential from the process environment."""

    return read_provider_credentials(ProviderId.SILICONFLOW, environ)


def read_chatanywhere_credentials(
    environ: Mapping[str, str],
) -> ProviderCredentials:
    """Read the ChatAnywhere credential from the process environment."""

    return read_provider_credentials(ProviderId.CHATANYWHERE, environ)


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def is_sensitive_name(value: str) -> bool:
    """Use one policy for environment names and structured fields."""
    normalized = _normalize_name(value)
    if normalized in {"input_tokens", "output_tokens"}:
        return False
    return _SENSITIVE_NAME_PATTERN.search(normalized) is not None


def credential_values(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Collect known secret values for exception and output redaction."""
    values: list[str] = []
    for name, value in environ.items():
        if not value:
            continue
        if is_sensitive_name(name):
            values.append(value)
            continue
        if name.upper() not in _PROXY_URL_ENV_NAMES:
            continue
        values.append(value)
        parsed = urlsplit(value)
        raw_userinfo = parsed.netloc.rpartition("@")[0]
        raw_username = parsed.username or ""
        raw_password = parsed.password or ""
        username = unquote(raw_username)
        password = unquote(raw_password)
        if raw_userinfo:
            values.extend((raw_userinfo, unquote(raw_userinfo)))
        if raw_username:
            values.append(raw_username)
        if username:
            values.append(username)
        if raw_password:
            values.append(raw_password)
        if username and password:
            values.append(f"{username}:{password}")
        if password:
            values.append(password)
    return tuple(dict.fromkeys(values))


def build_claude_environment(
    source: Mapping[str, str],
    credentials: ProviderCredentials,
    *,
    base_url: str,
    model_id: str,
    config_dir: Path,
) -> dict[str, str]:
    """Build an isolated child environment without retaining global providers."""
    normalized_base_url = validate_provider_base_url(credentials.provider, base_url)
    child = {key: source[key] for key in _SAFE_ENV_NAMES if key in source}
    child.update(
        {
            _CLAUDE_AUTH_ENV[credentials.provider]: credentials.api_key,
            "ANTHROPIC_BASE_URL": normalized_base_url,
            "ANTHROPIC_MODEL": model_id,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model_id,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model_id,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model_id,
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "HOME": str(config_dir.parent),
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
            if isinstance(key, str) and is_sensitive_name(key):
                cleaned[f"redacted_field_{len(cleaned)}"] = "[REDACTED]"
            else:
                cleaned[key] = redact_data(child, secrets)
        return cleaned  # type: ignore[return-value]
    if isinstance(value, tuple):
        return tuple(redact_data(item, secrets) for item in value)  # type: ignore[return-value]
    if isinstance(value, list):
        return [redact_data(item, secrets) for item in value]  # type: ignore[return-value]
    return value
