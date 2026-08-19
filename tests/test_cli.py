from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_ses_console_script_displays_help() -> None:
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
    assert "judge-calibration" in completed.stdout
    assert "skill-install" in completed.stdout
    assert completed.stderr == ""


def test_ses_console_script_runs_fixed_judge_calibration() -> None:
    executable = Path(sys.executable).with_name("ses")
    fixture = Path(__file__).parent / "fixtures" / "judges" / "calibration.json"

    completed = subprocess.run(
        [str(executable), "judge-calibration", "--fixture", str(fixture)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["fixed_offline_protocol_executed"] is True
    assert payload["live_model_measured"] is False
    assert payload["response_source"] == "course_authored_fixed_response"
    assert completed.stderr == ""
