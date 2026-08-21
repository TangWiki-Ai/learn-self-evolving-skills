from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ses.foundation.config import (
    ConfigurationError,
    ProviderId,
    RuntimeConfig,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import (
    CredentialError,
    build_claude_environment,
    credential_values,
    is_sensitive_name,
    read_provider_credentials,
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
            "chatanywhere_models_lock": "models.chatanywhere.lock.json",
            "data_manifest": "data/upstream/manifest.json",
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
            "provider": "siliconflow",
            "model": role,
        },
    )

    config = load_runtime_config(config_path)
    lock = load_model_lock(lock_path)

    assert config.models_lock_for(ProviderId.SILICONFLOW) == "models.lock.json"
    assert (
        config.models_lock_for(ProviderId.CHATANYWHERE)
        == "models.chatanywhere.lock.json"
    )
    assert lock.provider is ProviderId.SILICONFLOW
    assert lock.model.base_url == "https://api.siliconflow.cn/"
    assert "key" not in lock.model_dump_json().casefold()


def test_runtime_config_has_safe_project_relative_defaults() -> None:
    config = RuntimeConfig(schema_version="v1alpha1")

    assert config.models_lock == "models.lock.json"
    assert config.data_manifest == "data/upstream/manifest.json"


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


def test_models_lock_requires_one_https_model(tmp_path: Path) -> None:
    path = tmp_path / "models.lock.json"
    _write_json(
        path,
        {
            "schema_version": "v1alpha1",
            "engine": "claude-code",
            "engine_version": "2.1.220",
            "model": {
                "model_id": "model",
                "base_url": "http://provider.invalid/?api_key=secret",
            },
        },
    )

    with pytest.raises(ConfigurationError):
        load_model_lock(path)


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("siliconflow", "https://api.chatanywhere.tech/"),
        ("chatanywhere", "https://api.siliconflow.cn/"),
        ("siliconflow", "https://api.siliconflow.cn.evil.example/"),
        ("chatanywhere", "https://api.chatanywhere.tech/v1/"),
    ],
)
def test_model_lock_binds_provider_to_an_exact_official_base_url(
    tmp_path: Path, provider: str, base_url: str
) -> None:
    path = tmp_path / "models.lock.json"
    role = {"model_id": "model", "base_url": base_url}
    _write_json(
        path,
        {
            "schema_version": "v1alpha1",
            "engine": "claude-code",
            "engine_version": "2.1.220",
            "provider": provider,
            "model": role,
        },
    )

    with pytest.raises(ConfigurationError, match="not allowed"):
        load_model_lock(path)


@pytest.mark.parametrize(
    "base_url",
    ["https://api.chatanywhere.tech", "https://api.chatanywhere.org/"],
)
def test_chatanywhere_model_lock_accepts_both_official_hosts(
    tmp_path: Path, base_url: str
) -> None:
    path = tmp_path / "models.chatanywhere.lock.json"
    role = {"model_id": "claude-sonnet-4-6", "base_url": base_url}
    _write_json(
        path,
        {
            "schema_version": "v1alpha1",
            "engine": "claude-code",
            "engine_version": "2.1.220",
            "provider": "chatanywhere",
            "model": role,
        },
    )

    lock = load_model_lock(path)

    assert lock.provider is ProviderId.CHATANYWHERE
    assert lock.model.base_url == base_url.rstrip("/") + "/"


def test_credentials_only_come_from_environment_and_repr_is_safe() -> None:
    with pytest.raises(CredentialError, match="SILICONFLOW_API_KEY"):
        read_provider_credentials(ProviderId.SILICONFLOW, {})

    credentials = read_provider_credentials(
        ProviderId.SILICONFLOW, {"SILICONFLOW_API_KEY": "exact-process-secret"}
    )

    assert "exact-process-secret" not in repr(credentials)
    assert "REDACTED" in repr(credentials)
    assert credentials.provider is ProviderId.SILICONFLOW

    with pytest.raises(CredentialError, match="CHATANYWHERE_API_KEY"):
        read_provider_credentials(ProviderId.CHATANYWHERE, {})

    chatanywhere = read_provider_credentials(
        ProviderId.CHATANYWHERE,
        {"CHATANYWHERE_API_KEY": "chatanywhere-process-secret"},
    )
    assert chatanywhere.provider is ProviderId.CHATANYWHERE
    assert "chatanywhere-process-secret" not in repr(chatanywhere)


def test_claude_environment_removes_global_provider_state(tmp_path: Path) -> None:
    credentials = read_provider_credentials(
        ProviderId.SILICONFLOW, {"SILICONFLOW_API_KEY": "new-secret"}
    )

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
    assert child["CLAUDE_CODE_ATTRIBUTION_HEADER"] == "0"
    assert child["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert child["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"] == "1"


def test_claude_environment_preserves_explicit_proxy_route_and_redacts_it(
    tmp_path: Path,
) -> None:
    proxy_url = "http://proxy%2Duser:p%40ss%3Aword@127.0.0.1:7897"
    source = {
        "HTTPS_PROXY": proxy_url,
        "HTTP_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "NO_PROXY": "localhost,127.0.0.1",
    }
    credentials = read_provider_credentials(
        ProviderId.CHATANYWHERE, {"CHATANYWHERE_API_KEY": "chatanywhere-secret"}
    )

    child = build_claude_environment(
        source,
        credentials,
        base_url="https://api.chatanywhere.tech/",
        model_id="claude-sonnet-4-6",
        config_dir=tmp_path,
    )

    assert child["HTTPS_PROXY"] == proxy_url
    assert child["HTTP_PROXY"] == proxy_url
    assert child["ALL_PROXY"] == proxy_url
    assert child["NO_PROXY"] == "localhost,127.0.0.1"
    secrets = credential_values(source)
    assert proxy_url in secrets
    rendered = redact(
        f"proxy failed: {proxy_url}; "
        "raw_user=proxy%2Duser; user=proxy-user; "
        "raw_password=p%40ss%3Aword; password=p@ss:word; "
        "auth=proxy-user:p@ss:word",
        secrets,
    )
    assert proxy_url not in rendered
    assert "proxy%2Duser" not in rendered
    assert "proxy-user" not in rendered
    assert "p%40ss%3Aword" not in rendered
    assert "p@ss:word" not in rendered
    assert "proxy-user:p@ss:word" not in rendered


def test_chatanywhere_uses_only_anthropic_auth_token(tmp_path: Path) -> None:
    credentials = read_provider_credentials(
        ProviderId.CHATANYWHERE, {"CHATANYWHERE_API_KEY": "chatanywhere-secret"}
    )

    child = build_claude_environment(
        {
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "old-anthropic-key",
            "ANTHROPIC_AUTH_TOKEN": "old-anthropic-token",
            "SILICONFLOW_API_KEY": "siliconflow-secret",
            "CHATANYWHERE_API_KEY": "chatanywhere-secret",
        },
        credentials,
        base_url="https://api.chatanywhere.tech",
        model_id="claude-sonnet-4-6",
        config_dir=tmp_path,
    )

    assert child["ANTHROPIC_AUTH_TOKEN"] == "chatanywhere-secret"
    assert child["ANTHROPIC_BASE_URL"] == "https://api.chatanywhere.tech/"
    assert "ANTHROPIC_API_KEY" not in child
    assert "SILICONFLOW_API_KEY" not in child
    assert "CHATANYWHERE_API_KEY" not in child


def test_claude_environment_rejects_provider_endpoint_mismatch(
    tmp_path: Path,
) -> None:
    credentials = read_provider_credentials(
        ProviderId.CHATANYWHERE, {"CHATANYWHERE_API_KEY": "chatanywhere-secret"}
    )

    with pytest.raises(ValueError, match="not allowed"):
        build_claude_environment(
            {},
            credentials,
            base_url="https://api.siliconflow.cn/",
            model_id="model",
            config_dir=tmp_path,
        )


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
