from __future__ import annotations

from pathlib import Path

import pytest

from ses.cli.app import main

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_ROOT = PROJECT_ROOT / "fixtures/seed/capstone-shopping-assistant/profiles"


def test_fixed_profile_doctor_is_offline_and_reports_live_no_go(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "must-not-be-read")

    exit_code = main(
        [
            "doctor",
            "--project-root",
            str(PROJECT_ROOT),
            "--profile",
            str(PROFILE_ROOT / "fixed-v1.json"),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "shopping-fixed-v1" in output
    assert "10 source groups / 40 episode slots" in output
    assert "live release: no_go" in output
    assert "must-not-be-read" not in output


def test_live_profile_doctor_fails_closed_before_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SILICONFLOW_API_KEY", "must-not-be-read")

    exit_code = main(
        [
            "doctor",
            "--project-root",
            str(PROJECT_ROOT),
            "--profile",
            str(PROFILE_ROOT / "live-v1.json"),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "live source decision is no_go" in output
    assert "must-not-be-read" not in output
