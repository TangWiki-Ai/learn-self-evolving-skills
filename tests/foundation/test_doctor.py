from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from ses.contracts import (
    CompletedPayload,
    EngineEvent,
    EngineEventPayload,
    EngineExitStatus,
    ToolCallPayload,
    ToolResultPayload,
)
from ses.engines.events import make_event
from ses.foundation import doctor
from ses.foundation.config import ModelRole, ProviderId
from ses.foundation.credentials import ProviderCredentials

ROOT = Path(__file__).resolve().parents[2]


def test_doctor_offline_uses_local_manifest_and_skips_paid_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "check_claude", lambda executable="claude": "fake 1.0")

    results = doctor.run_doctor(
        project_root=ROOT,
        config_path=None,
        live=False,
        timeout=1,
        environ={"SILICONFLOW_API_KEY": "must-not-be-read"},
    )

    assert [result.name for result in results] == [
        "Python",
        "Claude Code",
        "Claude isolation",
        "Configuration",
        "Data",
        "Model",
        "MCP",
    ]
    assert next(result for result in results if result.name == "Data").status == "PASS"
    assert next(result for result in results if result.name == "Model").status == "SKIP"
    assert "must-not-be-read" not in repr(results)


def test_doctor_cli_never_echoes_environment_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(doctor, "check_claude", lambda executable="claude": "fake 1.0")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-supersecret123456")

    exit_code = doctor.main(["--project-root", str(ROOT)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "sk-supersecret123456" not in output
    assert "Data: 3 个固定 benchmark source" in output


def test_doctor_rejects_nonpositive_timeout() -> None:
    with pytest.raises(SystemExit):
        doctor.main(["--timeout", "0"])


def test_doctor_parser_accepts_provider_selection() -> None:
    args = doctor.build_parser().parse_args(["--provider", "chatanywhere"])

    assert args.provider == "chatanywhere"


def test_offline_doctor_selects_chatanywhere_lock_without_reading_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "schema_version": "v1alpha1",
        "models_lock": "models.lock.json",
        "chatanywhere_models_lock": "models.chatanywhere.lock.json",
        "default_provider": "siliconflow",
    }
    role = {
        "model_id": "claude-sonnet-4-6",
        "base_url": "https://api.chatanywhere.tech/",
    }
    (tmp_path / "ses.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "models.chatanywhere.lock.json").write_text(
        json.dumps(
            {
                "schema_version": "v1alpha1",
                "engine": "claude-code",
                "engine_version": "2.1.220",
                "provider": "chatanywhere",
                "roles": {name.value: role for name in ModelRole},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "check_local_data", lambda root, config=None: "ok")
    monkeypatch.setattr(
        doctor, "check_claude", lambda executable="claude": "/fake/claude (2.1.220)"
    )

    def must_not_read_credentials(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("offline doctor read provider credentials")

    monkeypatch.setattr(doctor, "read_provider_credentials", must_not_read_credentials)

    results = doctor.run_doctor(
        project_root=tmp_path,
        config_path=tmp_path / "ses.json",
        live=False,
        timeout=1,
        environ={},
        provider=ProviderId.CHATANYWHERE,
    )

    configuration = next(result for result in results if result.name == "Configuration")
    assert configuration.status == "PASS"
    assert "chatanywhere" in configuration.detail
    assert next(result for result in results if result.name == "Model").status == "SKIP"


def test_live_doctor_reads_selected_provider_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "schema_version": "v1alpha1",
        "chatanywhere_models_lock": "models.chatanywhere.lock.json",
        "default_provider": "chatanywhere",
    }
    role = {
        "model_id": "claude-sonnet-4-6",
        "base_url": "https://api.chatanywhere.tech/",
    }
    (tmp_path / "ses.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "models.chatanywhere.lock.json").write_text(
        json.dumps(
            {
                "schema_version": "v1alpha1",
                "engine": "claude-code",
                "engine_version": "2.1.220",
                "provider": "chatanywhere",
                "roles": {name.value: role for name in ModelRole},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "check_local_data", lambda root, config=None: "ok")
    monkeypatch.setattr(
        doctor, "check_claude", lambda executable="claude": "/fake/claude (2.1.220)"
    )
    observed: dict[str, object] = {}

    async def fake_live(
        **kwargs: object,
    ) -> tuple[doctor.CheckResult, doctor.CheckResult]:
        observed.update(kwargs)
        return (
            doctor.CheckResult("PASS", "Model", "ok"),
            doctor.CheckResult("PASS", "MCP", "ok"),
        )

    monkeypatch.setattr(doctor, "_live_model_and_mcp", fake_live)

    results = doctor.run_doctor(
        project_root=tmp_path,
        config_path=tmp_path / "ses.json",
        live=True,
        timeout=1,
        environ={"CHATANYWHERE_API_KEY": "ordinary-chatanywhere-secret"},
    )

    assert all(result.status in {"PASS", "WARN"} for result in results)
    credentials = observed["credentials"]
    assert isinstance(credentials, ProviderCredentials)
    assert credentials.provider is ProviderId.CHATANYWHERE


def test_live_doctor_requires_config_and_model_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "check_claude", lambda executable="claude": "fake 1.0")

    results = doctor.run_doctor(
        project_root=ROOT,
        config_path=None,
        live=True,
        timeout=1,
        environ={"SILICONFLOW_API_KEY": "ordinary-live-secret"},
    )

    model = next(result for result in results if result.name == "Model")
    assert model.status == "FAIL"
    assert "config" in model.detail.casefold() or "lock" in model.detail.casefold()
    assert "ordinary-live-secret" not in repr(results)


def test_live_doctor_rejects_claude_version_different_from_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "schema_version": "v1alpha1",
        "models_lock": "models.lock.json",
        "data_manifest": "data/upstream/manifest.json",
        "workspace_root": ".ses/workspaces",
        "claude_executable": "claude",
    }
    role = {
        "model_id": "locked-model",
        "base_url": "https://api.siliconflow.cn/",
    }
    (tmp_path / "ses.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "models.lock.json").write_text(
        json.dumps(
            {
                "schema_version": "v1alpha1",
                "engine": "claude-code",
                "engine_version": "2.1.220",
                "roles": {name.value: role for name in ModelRole},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "check_local_data", lambda root, config=None: "ok")
    monkeypatch.setattr(
        doctor, "check_claude", lambda executable="claude": "/fake/claude (9.9.9)"
    )

    async def must_not_run(**kwargs: object) -> object:
        del kwargs
        raise AssertionError("live model ran despite version mismatch")

    monkeypatch.setattr(doctor, "_live_model_and_mcp", must_not_run)

    results = doctor.run_doctor(
        project_root=tmp_path,
        config_path=tmp_path / "ses.json",
        live=True,
        timeout=1,
        environ={"SILICONFLOW_API_KEY": "ordinary-live-secret"},
    )

    model = next(result for result in results if result.name == "Model")
    assert model.status == "FAIL"
    assert "2.1.220" in model.detail
    assert "9.9.9" in model.detail


def test_doctor_exception_message_redacts_known_plain_secret() -> None:
    def fail() -> str:
        raise RuntimeError("provider failed with ordinary-exception-secret")

    result = doctor._run_check("Provider", fail, secrets=("ordinary-exception-secret",))

    assert result.status == "FAIL"
    assert "ordinary-exception-secret" not in result.detail


def _mcp_events(
    *,
    tool_name: str = doctor.MCP_TOOL_NAME,
    result_call_id: str = "tool-1",
    is_error: bool = False,
    content: JsonValue = doctor.PING_RESULT,
) -> list[EngineEvent]:
    payloads: tuple[EngineEventPayload, ...] = (
        ToolCallPayload(
            message_id="message-1",
            tool_call_id="tool-1",
            tool_name=tool_name,
            arguments={"value": doctor.PING_VALUE},
        ),
        ToolResultPayload(
            tool_call_id=result_call_id,
            content=content,
            is_error=is_error,
        ),
        CompletedPayload(
            exit_status=EngineExitStatus.SUCCESS,
            session_id="session-1",
        ),
    )
    return [
        make_event(request_id="request-1", sequence=index, payload=payload)
        for index, payload in enumerate(payloads)
    ]


def test_doctor_validates_exact_mcp_exchange() -> None:
    result = doctor.validate_mcp_exchange(_mcp_events())

    assert result.status == "PASS"


def test_doctor_accepts_exact_mcp_text_block_pong() -> None:
    result = doctor.validate_mcp_exchange(
        _mcp_events(content=[{"type": "text", "text": doctor.PING_RESULT}])
    )

    assert result.status == "PASS"


@pytest.mark.parametrize(
    "events",
    [
        _mcp_events(tool_name="mcp__wrong__phase0_ping"),
        _mcp_events(result_call_id="tool-other"),
        _mcp_events(is_error=True),
        _mcp_events(content="pong:wrong"),
    ],
)
def test_doctor_rejects_wrong_or_failed_mcp_exchange(
    events: list[EngineEvent],
) -> None:
    result = doctor.validate_mcp_exchange(events)

    assert result.status == "FAIL"
