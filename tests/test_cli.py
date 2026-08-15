from __future__ import annotations

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
    assert completed.stderr == ""
