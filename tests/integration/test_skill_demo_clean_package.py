from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONNOUSERSITE": "1",
            "UV_CACHE_DIR": "/tmp/ses-clean-package-uv-cache",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_skill_demo_runs_from_an_installed_wheel_without_the_repository(
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "dist"
    built = _run(["uv", "build", "--wheel", "--out-dir", str(distribution)], cwd=ROOT)
    assert built.returncode == 0, built.stderr
    wheel = next(distribution.glob("*.whl"))

    environment = tmp_path / "environment"
    created = _run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(environment)],
        cwd=tmp_path,
    )
    assert created.returncode == 0, created.stderr
    installed = _run(
        [
            str(environment / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(wheel),
        ],
        cwd=tmp_path,
    )
    assert installed.returncode == 0, installed.stderr

    isolated_working_directory = tmp_path / "outside-repository"
    isolated_working_directory.mkdir()
    completed = _run(
        [
            str(environment / "bin" / "ses"),
            "skill-demo",
            "--reference",
            "--output-root",
            str(isolated_working_directory / "demo"),
            "--json",
        ],
        cwd=isolated_working_directory,
    )

    assert completed.returncode == 0, completed.stderr
    comparison = json.loads(completed.stdout)
    assert comparison["source"]["kind"] == "current_run"
    assert comparison["skill"]["source"] == "reference"
    assert comparison["runs"]["with_skill"]["outcome"] == "pass"
