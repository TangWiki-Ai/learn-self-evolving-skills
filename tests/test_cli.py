from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_ses_console_script_only_exposes_the_journey() -> None:
    executable = Path(sys.executable).with_name("ses")

    completed = subprocess.run(
        [str(executable), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert "usage: ses" in completed.stdout
    assert "journey" in completed.stdout
    assert "judge-calibration" not in completed.stdout
    assert "auto-evolve" not in completed.stdout
    assert completed.stderr == ""


def test_ses_console_script_displays_the_journey_actions() -> None:
    executable = Path(sys.executable).with_name("ses")

    completed = subprocess.run(
        [str(executable), "journey", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert "station" in completed.stdout
    assert "dashboard" in completed.stdout
    assert "status" in completed.stdout
    assert "--host" not in completed.stdout
    assert completed.stderr == ""


def test_dashboard_cli_does_not_offer_a_network_host_override() -> None:
    executable = Path(sys.executable).with_name("ses")

    completed = subprocess.run(
        [str(executable), "journey", "dashboard", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert "--port" in completed.stdout
    assert "--host" not in completed.stdout
    assert completed.stderr == ""
