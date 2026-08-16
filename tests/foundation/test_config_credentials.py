from __future__ import annotations

import json
from pathlib import Path

import pytest

from ses.foundation.config import (
    ConfigurationError,
    ModelRole,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import (
    CredentialError,
    build_claude_environment,
    read_siliconflow_credentials,
    redact,
    redact_data,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_config_and_models_lock_are_strict_and_credential_free(tmp_path: Path) -> None:
    config_path = tmp_path / "ses.json"
    lock_path = tmp_path / "models.lock.json"
    _write_json(
        config_path,
        {
            "schema_version": "v1alpha1",
            "models_lock": "models.lock.json",
            "data_manifest": "data/upstream/manifest.json",
            "workspace_root": ".ses/workspaces",
            "claude_executable": "claude",
        },
    )
    role = {
        "model_id": "deepseek-ai/DeepSeek-V3.2",
        "base_url": "https://api.siliconflow.cn",
    }
    _write_json(
        lock_path,
        {
            "schema_version": "v1alpha1",
            "engine": "claude-code",
            "engine_version": "2.1.220",
            "roles": {name.value: role for name in ModelRole},
        },
    )

    config = load_runtime_config(config_path)
    lock = load_model_lock(lock_path)

    assert config.workspace_root == ".ses/workspaces"
    assert lock.roles[ModelRole.MAIN].base_url == "https://api.siliconflow.cn/"
    assert "key" not in lock.model_dump_json().casefold()
    with pytest.raises(TypeError):
        lock.roles[ModelRole.MAIN] = role  # type: ignore[index]


@pytest.mark.parametrize(
    "value",
    [
        {"schema_version": "v1alpha1", "unknown": True},
        {"schema_version": "v1alpha1", "workspace_root": "../escape"},
        {"schema_version": "v1alpha1", "models_lock": "/absolute/lock"},
    ],
)
def test_runtime_config_rejects_unknown_and_unsafe_fields(
    tmp_path: Path, value: object
) -> None:
    path = tmp_path / "ses.json"
    _write_json(path, value)

    with pytest.raises(ConfigurationError):
        load_runtime_config(path)


def test_models_lock_requires_every_role_and_https(tmp_path: Path) -> None:
    path = tmp_path / "models.lock.json"
    _write_json(
        path,
        {
            "schema_version": "v1alpha1",
            "engine": "claude-code",
            "engine_version": "2.1.220",
            "roles": {
                "main": {
                    "model_id": "model",
                    "base_url": "http://provider.invalid/?api_key=secret",
                }
            },
        },
    )

    with pytest.raises(ConfigurationError):
        load_model_lock(path)


def test_credentials_only_come_from_environment_and_repr_is_safe() -> None:
    with pytest.raises(CredentialError, match="SILICONFLOW_API_KEY"):
        read_siliconflow_credentials({})

    credentials = read_siliconflow_credentials(
        {"SILICONFLOW_API_KEY": "exact-process-secret"}
    )

    assert "exact-process-secret" not in repr(credentials)
    assert "REDACTED" in repr(credentials)


def test_claude_environment_removes_global_provider_state(tmp_path: Path) -> None:
    credentials = read_siliconflow_credentials({"SILICONFLOW_API_KEY": "new-secret"})

    child = build_claude_environment(
        {
            "PATH": "/usr/bin",
            "ANTHROPIC_AUTH_TOKEN": "old-secret",
            "ANTHROPIC_BASE_URL": "https://old.invalid/",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "SILICONFLOW_API_KEY": "new-secret",
        },
        credentials,
        base_url="https://api.siliconflow.cn/",
        model_id="locked-model",
        config_dir=tmp_path,
    )

    assert child["PATH"] == "/usr/bin"
    assert child["ANTHROPIC_API_KEY"] == "new-secret"
    assert child["ANTHROPIC_BASE_URL"] == "https://api.siliconflow.cn/"
    assert child["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert "ANTHROPIC_AUTH_TOKEN" not in child
    assert "CLAUDE_CODE_USE_VERTEX" not in child
    assert "SILICONFLOW_API_KEY" not in child


def test_redaction_covers_nested_fields_headers_and_known_values() -> None:
    secret = "exact-process-secret"
    value = {
        "message": f"failed with {secret}; Authorization: Bearer bearer-value",
        "nested": [{"apiKey": "another-value", "safe": "sk-example123456789"}],
    }

    cleaned = redact_data(value, (secret,))
    rendered = json.dumps(cleaned)

    assert secret not in rendered
    assert "bearer-value" not in rendered
    assert "another-value" not in rendered
    assert "sk-example123456789" not in rendered
    assert redact("x-api-key=abc", ()) == "x-api-key=[REDACTED]"
