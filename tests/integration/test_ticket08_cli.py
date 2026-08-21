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
            "--provider",
            "chatanywhere",
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
    assert payload["seed_review_status"] == "course_authored_pending_human_review"
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
    assert "chatanywhere_api_key" not in combined
    assert "siliconflow_api_key" not in combined
    assert "sk-" not in combined
    assert str(ROOT).casefold() not in (output / "l2.html").read_text().casefold()


def test_ticket08_live_mode_rejects_pending_seed_review_before_provider_use(
    tmp_path: Path,
) -> None:
    environment = _environment()
    environment["SILICONFLOW_API_KEY"] = "must-not-be-used"
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
            "--provider",
            "chatanywhere",
            "--creator-timeout",
            "0.1",
            "--json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "independent signed human review" in completed.stderr
    assert not (tmp_path / "live" / "paired-comparison.json").exists()


def test_create_v0_live_rejects_pending_seed_review(tmp_path: Path) -> None:
    environment = _environment()
    environment["SILICONFLOW_API_KEY"] = "must-not-be-used"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ses.cli.app",
            "skill",
            "create-v0",
            "--out",
            str(tmp_path / "v0"),
            "--mode",
            "live",
            "--provider",
            "chatanywhere",
            "--json",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1
    assert "independent signed human review" in completed.stderr
    assert not (tmp_path / "v0").exists()


def test_fixed_skill_subcommands_accept_chatanywhere_without_credentials(
    tmp_path: Path,
) -> None:
    skill = ROOT / "fixtures" / "seed" / "skill" / "v0"
    commands = (
        (
            "skill",
            "create-v0",
            "--out",
            str(tmp_path / "created"),
            "--mode",
            "fixed",
            "--provider",
            "chatanywhere",
            "--json",
        ),
        (
            "trigger-eval",
            "--skill",
            str(skill),
            "--mode",
            "fixed",
            "--provider",
            "chatanywhere",
            "--json",
        ),
        (
            "paired-comparison",
            "--skill",
            str(skill),
            "--output-root",
            str(tmp_path / "paired"),
            "--mode",
            "fixed",
            "--provider",
            "chatanywhere",
            "--json",
        ),
    )

    for command in commands:
        completed = subprocess.run(
            [sys.executable, "-m", "ses.cli.app", *command],
            cwd=ROOT,
            env=_environment(),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert "api_key" not in (completed.stdout + completed.stderr).casefold()
