from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ses.foundation.config import (
    ConfigurationError,
    ModelRole,
    RuntimeConfig,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import (
    CredentialError,
    build_claude_environment,
    credential_values,
    is_sensitive_name,
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


def test_runtime_config_defaults_to_system_temporary_workspaces() -> None:
    config = RuntimeConfig(schema_version="v1alpha1")

    assert config.workspace_root is None


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
            "OPENAI_API_KEY": "openai-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "GITHUB_TOKEN": "github-secret",
            "SHOP_API_KEY": "shop-secret",
            "UNRELATED_VALUE": "must-not-inherit",
            "LANG": "en_US.UTF-8",
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
    assert "OPENAI_API_KEY" not in child
    assert "AWS_SECRET_ACCESS_KEY" not in child
    assert "GITHUB_TOKEN" not in child
    assert "SHOP_API_KEY" not in child
    assert "UNRELATED_VALUE" not in child
    assert child["LANG"] == "en_US.UTF-8"
    assert child["HOME"] == str(tmp_path.parent)


def test_sensitive_name_detection_and_value_collection_share_one_policy() -> None:
    environment = {
        "OPENAI_API_KEY": "ordinary-openai-secret",
        "AWS_SESSION_TOKEN": "ordinary-aws-secret",
        "GH_TOKEN": "ordinary-github-secret",
        "SHOP_PASSWORD": "ordinary-shop-secret",
        "PATH": "/usr/bin",
    }

    assert all(is_sensitive_name(name) for name in environment if name != "PATH")
    assert set(credential_values(environment)) == {
        "ordinary-openai-secret",
        "ordinary-aws-secret",
        "ordinary-github-secret",
        "ordinary-shop-secret",
    }


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


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": "v1alpha1", "password": "plain-string-secret"},
        {
            "schema_version": "v1alpha1",
            "nested": {"credentials": {"value": "nested-plain-secret"}},
        },
    ],
)
def test_config_validation_errors_never_include_input_values(
    tmp_path: Path, document: object
) -> None:
    path = tmp_path / "ses.json"
    _write_json(path, document)

    with pytest.raises(ConfigurationError) as captured:
        load_runtime_config(path)

    message = str(captured.value)
    assert "plain-string-secret" not in message
    assert "nested-plain-secret" not in message
    assert "input_value" not in message


def test_direct_pydantic_error_hides_plain_string_input() -> None:
    with pytest.raises(ValidationError) as captured:
        RuntimeConfig.model_validate(
            {"schema_version": "v1alpha1", "password": "direct-plain-secret"}
        )

    assert "direct-plain-secret" not in str(captured.value)
    assert "input_value" not in str(captured.value)
