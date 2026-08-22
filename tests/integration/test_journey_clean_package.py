from __future__ import annotations

import os
import subprocess
import sys
import zipfile
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


def test_wheel_contains_only_journey_runtime_and_exposes_cli_help(
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "dist"
    built = _run(["uv", "build", "--wheel", "--out-dir", str(distribution)], cwd=ROOT)
    assert built.returncode == 0, built.stderr
    wheel = next(distribution.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    assert "ses/shop/mcp_server.py" in names
    assert not any(name.endswith(".md") for name in names)
    assert not any("/automation/" in name for name in names)
    assert not any("/evolution/" in name for name in names)
    assert not any("/testset/" in name for name in names)

    # The model locks, benchmark catalog, and instructor are repo assets. This
    # wheel smoke checks code packaging and command discovery, not a standalone
    # Journey run outside a clone.
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
            "journey",
            "--help",
        ],
        cwd=isolated_working_directory,
    )

    assert completed.returncode == 0, completed.stderr
    assert "station" in completed.stdout
    assert "dashboard" in completed.stdout
    assert "status" in completed.stdout
