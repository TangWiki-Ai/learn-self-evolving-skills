"""Validation for public JSON carried by contracts."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping

# These combinations remain sensitive inside longer qualified field names.
_SENSITIVE_FIELD_COMBINATIONS = frozenset(
    {
        "access_token",
        "api_key",
        "api_token",
        "apikey",
        "auth_header",
        "auth_token",
        "authentication_header",
        "authentication_token",
        "authorization_header",
        "authorization_token",
        "bearer_token",
        "client_secret",
        "final_answer",
        "final_gold",
        "gold_answer",
        "headers_map",
        "hidden_answer",
        "hidden_gold",
        "http_headers",
        "id_token",
        "private_answer",
        "private_key",
        "proxy_authorization",
        "reference_answer",
        "reference_trace",
        "reference_trajectory",
        "refresh_token",
        "request_header",
        "request_headers",
        "response_header",
        "response_headers",
        "secret_key",
        "selection_answer",
        "selection_gold",
        "session_token",
        "set_cookie",
        "x_api_key",
    }
)
# These names unambiguously describe credential material wherever they appear as a
# complete normalized token. Token boundaries avoid matching words such as secretary.
_SENSITIVE_FIELD_TOKENS = frozenset(
    {
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "passwd",
        "password",
        "secret",
        "secrets",
    }
)
# These names are ambiguous in business data and are sensitive only when complete.
_AMBIGUOUS_EXACT_FIELD_NAMES = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "gold",
        "headers",
        "token",
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
    if normalized in _AMBIGUOUS_EXACT_FIELD_NAMES:
        return True
    tokens = frozenset(normalized.split("_"))
    if tokens & _SENSITIVE_FIELD_TOKENS:
        return True
    padded = f"_{normalized}_"
    return any(
        f"_{combination}_" in padded for combination in _SENSITIVE_FIELD_COMBINATIONS
    )


def _validate_utf8(value: str, path: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(f"string at {path} must be valid UTF-8") from error


def validate_public_data(value: object, path: str = "$") -> None:
    """Reject credentials, private answers, invalid UTF-8, and invalid JSON floats."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                _validate_utf8(key, f"{path}.<key>")
                if _is_forbidden_field_name(key):
                    raise ValueError(f"forbidden field {key!r} at {path}")
            validate_public_data(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            validate_public_data(child, f"{path}[{index}]")
    elif isinstance(value, str):
        _validate_utf8(value, path)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite float at {path} is not valid canonical JSON")
