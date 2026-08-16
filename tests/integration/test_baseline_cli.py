from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CASE_ID = "state-bench-customer-support-2-return-defective-electronics"
ROOT = Path(__file__).parents[2]


def _run(output_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if "API_KEY" in name or "TOKEN" in name:
            environment.pop(name)
    environment["HTTP_PROXY"] = "http://127.0.0.1:9"
    environment["HTTPS_PROXY"] = "http://127.0.0.1:9"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ses.cli.baseline",
            "--output-root",
            str(output_root),
            *args,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_python_module_runs_repeated_offline_baseline_and_renders_html(
    tmp_path: Path,
) -> None:
    completed = _run(
        tmp_path,
        "--run-id",
        "run-cli-baseline",
        "--iterations",
        "2",
        "--json",
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["run_id"] == "run-cli-baseline"
    assert payload["metrics"] == {
        "sample_size": 1,
        "iteration_sample_size": 2,
        "pass_at_1": 1.0,
        "pass_power_k": 1.0,
        "k": 2,
    }
    html_path = tmp_path / "run-cli-baseline" / "l1.html"
    assert payload["html"] == str(html_path)
    assert html_path.exists()
    events = (tmp_path / "run-cli-baseline" / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert CASE_ID in events
    assert "API_KEY" not in events
    assert "http://127.0.0.1:9" not in events


def test_resume_is_idempotent_and_explicit_rerun_appends_a_new_iteration(
    tmp_path: Path,
) -> None:
    first = _run(tmp_path, "--run-id", "run-cli-resume", "--json")
    assert first.returncode == 0, first.stderr
    events_path = tmp_path / "run-cli-resume" / "events.jsonl"
    prefix = events_path.read_bytes()

    resumed = _run(
        tmp_path,
        "--run-id",
        "run-cli-resume",
        "--resume",
        "--json",
    )
    assert resumed.returncode == 0, resumed.stderr
    assert events_path.read_bytes() == prefix

    rerun = _run(
        tmp_path,
        "--run-id",
        "run-cli-resume",
        "--resume",
        "--rerun",
        CASE_ID,
        "--json",
    )
    assert rerun.returncode == 0, rerun.stderr
    assert events_path.read_bytes().startswith(prefix)
    assert '"iteration_id":"iteration-1"' in events_path.read_text(encoding="utf-8")


def test_cli_returns_structured_budget_stop_with_partial_result(tmp_path: Path) -> None:
    completed = _run(
        tmp_path,
        "--run-id",
        "run-cli-budget",
        "--max-input-tokens",
        "1",
        "--json",
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["stop_reason"] == "input_token_limit"
    events = (tmp_path / "run-cli-budget" / "events.jsonl").read_text(encoding="utf-8")
    assert '"partial_result"' in events
    assert '"status":"budget_stop"' in events
