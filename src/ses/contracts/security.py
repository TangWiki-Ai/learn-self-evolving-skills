"""Validation for public JSON carried by contracts."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping

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
