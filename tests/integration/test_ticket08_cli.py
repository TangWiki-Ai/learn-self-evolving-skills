from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if "API_KEY" in name or "TOKEN" in name:
            environment.pop(name)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["HTTP_PROXY"] = "http://127.0.0.1:9"
    environment["HTTPS_PROXY"] = "http://127.0.0.1:9"
    return environment


def test_ticket08_cli_runs_full_vertical_slice_offline(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ses.cli.app",
            "skill-v0-pipeline",
            "--output-root",
            str(tmp_path / "ticket08"),
            "--json",
        ],
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["mode"] == "fixed"
    assert payload["creator_measurement"] == "synthetic_offline"
    assert payload["trigger_measurement"] == "synthetic_offline"
    assert payload["paired_measurement"] == "synthetic_offline"
    assert payload["seed_count"] == 9
    assert payload["static_gate"] == "pass"
    assert payload["trigger_precision"] == 1.0
    assert payload["trigger_recall"] == 1.0
    assert payload["paired_case_count"] == 15
    output = tmp_path / "ticket08"
    for relative in (
        "skill/v0/SKILL.md",
        "skill/v0/skill-manifest.json",
        "static-gate.json",
        "trigger-eval.json",
        "paired-comparison.json",
        "l2.html",
        "summary.json",
    ):
        assert (output / relative).is_file(), relative
    combined = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in output.rglob("*")
        if path.is_file()
    ).casefold()
    assert "siliconflow_api_key" not in combined
    assert "sk-" not in combined
    assert str(ROOT).casefold() not in (output / "l2.html").read_text().casefold()


def test_ticket08_live_mode_requires_environment_key(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ses.cli.app",
            "skill-v0-pipeline",
            "--output-root",
            str(tmp_path / "live"),
            "--mode",
            "live",
            "--json",
        ],
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "missing SILICONFLOW_API_KEY" in completed.stderr
    assert not (tmp_path / "live" / "paired-comparison.json").exists()
