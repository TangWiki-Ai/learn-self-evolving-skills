from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ses.runner import develop_catalog_sha256, load_develop_catalog

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


def test_cli_runs_catalog_case_through_multiturn_judges_and_links_artifacts(
    tmp_path: Path,
) -> None:
    completed = _run(
        tmp_path,
        "--run-id",
        "run-cli-pipeline",
        "--json",
    )

    assert completed.returncode == 0, completed.stderr
    events_path = tmp_path / "run-cli-pipeline" / "events.jsonl"
    records = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert records[0]["config"]["data_version"] == develop_catalog_sha256(
        load_develop_catalog()
    )
    result = next(record for record in records if record["event_type"] == "attempt")
    assert result["status"] == "pass"
    assert result["turn_count"] == 2
    assert result["session_resumed"] is True
    assert result["artifacts"]["traces"]
    assert result["artifacts"]["state_diff"]
    assert result["artifacts"]["grade"]

    run_dir = tmp_path / "run-cli-pipeline"
    artifact_paths = [
        *(item["path"] for item in result["artifacts"]["traces"]),
        result["artifacts"]["state_diff"]["path"],
        result["artifacts"]["grade"]["path"],
    ]
    payloads = [json.loads((run_dir / path).read_text()) for path in artifact_paths]
    assert [payload["record_type"] for payload in payloads] == [
        "trace",
        "trace",
        "state_diff",
        "case_grade",
    ]
    grade = payloads[-1]
    assert grade["status"] == "pass"
    assert {assertion["judge"] for assertion in grade["assertions"]} == {
        "state",
        "rule",
    }

    workspaces = list((run_dir / "workspaces").glob("case-*/workspace"))
    assert len(workspaces) == 1
    html = (run_dir / "l1.html").read_text(encoding="utf-8")
    assert 'href="artifacts/' in html


def test_public_l1_artifacts_do_not_leak_private_fields_credentials_or_local_paths(
    tmp_path: Path,
) -> None:
    completed = _run(tmp_path, "--run-id", "run-cli-no-leak", "--json")
    assert completed.returncode == 0, completed.stderr

    run_dir = tmp_path / "run-cli-no-leak"
    public = "\n".join(
        path.read_text(encoding="utf-8")
        for path in run_dir.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl", ".html"}
    )
    lowered = public.casefold()
    assert "hidden_gold" not in lowered
    assert "gold_answer" not in lowered
    assert "api_key" not in lowered
    assert "bearer " not in lowered
    assert str(tmp_path) not in public


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


def test_cli_stops_before_the_second_turn_and_keeps_the_partial_trace(
    tmp_path: Path,
) -> None:
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
    events = [
        json.loads(line)
        for line in (tmp_path / "run-cli-budget" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    attempt = next(event for event in events if event["event_type"] == "attempt")
    assert attempt["status"] == "budget_stop"
    assert len(attempt["artifacts"]["traces"]) == 1
    assert attempt["artifacts"]["grade"] is None
    assert attempt["usage"]["input_tokens"] > 1
    assert events[-1]["status"] == "budget_stop"
    assert events[-1]["event_type"] == "budget_stop"
