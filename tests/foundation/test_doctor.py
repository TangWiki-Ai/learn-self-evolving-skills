from __future__ import annotations

from pathlib import Path

import pytest

from ses.foundation import doctor

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
