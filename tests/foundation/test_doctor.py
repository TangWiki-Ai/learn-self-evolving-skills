from __future__ import annotations

import json
from pathlib import Path

import pytest

from ses.foundation import doctor
from ses.foundation.config import ProviderId

ROOT = Path(__file__).resolve().parents[2]


def test_journey_doctor_checks_local_prerequisites_without_reading_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor, "check_claude", lambda executable="claude": "/fake/claude (2.1.220)"
    )

    results = doctor.run_doctor(
        project_root=ROOT,
        config_path=ROOT / "ses.json",
        environ={"SILICONFLOW_API_KEY": "must-not-be-read"},
    )

    assert [result.name for result in results] == [
        "Python",
        "Claude Code",
        "Claude isolation",
        "Configuration",
        "Data",
    ]
    assert all(result.status in {"PASS", "WARN"} for result in results)
    assert "must-not-be-read" not in repr(results)


def test_doctor_selects_chatanywhere_lock_without_reading_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "schema_version": "v1alpha1",
        "models_lock": "models.lock.json",
        "chatanywhere_models_lock": "models.chatanywhere.lock.json",
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
                "model": role,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "check_local_data", lambda root, config: "ok")
    monkeypatch.setattr(
        doctor, "check_claude", lambda executable="claude": "/fake/claude (2.1.220)"
    )

    results = doctor.run_doctor(
        project_root=tmp_path,
        config_path=tmp_path / "ses.json",
        environ={"CHATANYWHERE_API_KEY": "must-not-be-read"},
        provider=ProviderId.CHATANYWHERE,
    )

    configuration = next(result for result in results if result.name == "Configuration")
    assert configuration.status == "PASS"
    assert "chatanywhere" in configuration.detail
    assert "must-not-be-read" not in repr(results)


def test_doctor_exception_message_redacts_known_plain_secret() -> None:
    def fail() -> str:
        raise RuntimeError("provider failed with ordinary-exception-secret")

    result = doctor._run_check("Provider", fail, secrets=("ordinary-exception-secret",))

    assert result.status == "FAIL"
    assert "ordinary-exception-secret" not in result.detail


def test_doctor_reports_invalid_configuration_without_checking_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ses.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(doctor, "check_claude", lambda executable="claude": "fake")

    results = doctor.run_doctor(
        project_root=tmp_path,
        config_path=tmp_path / "ses.json",
        environ={},
    )

    assert (
        next(item for item in results if item.name == "Configuration").status == "FAIL"
    )
    assert next(item for item in results if item.name == "Data").status == "SKIP"
