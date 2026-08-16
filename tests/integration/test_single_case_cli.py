from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from ses.contracts import EngineExitStatus, GradeStatus
from ses.evaluator import (
    RunOutcome,
    SingleCaseRunError,
    classify_run_outcome,
    run_pinned_case,
)
from ses.foundation.config import ModelRole, load_model_lock, load_runtime_config

ROOT = Path(__file__).parents[2]
CASE_ID = "state-bench-customer-support-2-return-defective-electronics"


def _run_cli(output_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["SILICONFLOW_API_KEY"] = "ordinary-integration-secret"
    environment["HTTP_PROXY"] = "http://127.0.0.1:9"
    environment["HTTPS_PROXY"] = "http://127.0.0.1:9"
    return subprocess.run(
        [sys.executable, "-m", "ses.cli.app", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def test_cli_runs_fresh_offline_case_and_inspects_complete_l1_result(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "runs"
    first = _run_cli(
        output_root,
        "run-case",
        "--output-root",
        str(output_root),
        "--json",
    )
    second = _run_cli(
        output_root,
        "run-case",
        "--output-root",
        str(output_root),
        "--json",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    result = json.loads(first.stdout)
    second_result = json.loads(second.stdout)
    assert result["run_id"] != second_result["run_id"]
    assert result["case_id"] == CASE_ID
    assert result["outcome"] == "pass"
    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
        "assistant",
    ]
    assert [call["tool_name"] for call in result["tool_calls"]] == [
        "get_order",
        "get_policies",
        "process_return",
        "process_return",
    ]
    assert all(call["output"] is not None for call in result["tool_calls"])
    assert result["state_diff"]["changed"]
    assert {assertion["judge"] for assertion in result["assertions"]} == {
        "state",
        "rule",
    }
    assert {assertion["status"] for assertion in result["assertions"]} == {"pass"}
    assert result["usage"] == {
        "input_tokens": 137,
        "output_tokens": 61,
        "cost_amount": None,
        "cost_currency": None,
    }
    assert result["skill"] == {"version": None, "sha256": None}
    assert "mappingproxy" not in json.dumps(result)

    run_dir = output_root / result["run_id"]
    workspaces = list((run_dir / "workspaces").glob("case-*/workspace"))
    second_workspaces = list(
        (output_root / second_result["run_id"] / "workspaces").glob("case-*/workspace")
    )
    assert len(workspaces) == len(second_workspaces) == 1
    assert workspaces[0] != second_workspaces[0]
    for reference_value in result["artifacts"].values():
        reference = _mapping(reference_value)
        path = run_dir / str(reference["path"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
    assert "ordinary-integration-secret" not in (run_dir / "result.json").read_text(
        encoding="utf-8"
    )

    inspected = _run_cli(
        output_root,
        "inspect",
        result["run_id"],
        CASE_ID,
        "--output-root",
        str(output_root),
        "--json",
    )
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout) == result

    trace_reference = _mapping(result["artifacts"]["trace"])
    trace_path = run_dir / str(trace_reference["path"])
    trace_path.write_bytes(trace_path.read_bytes() + b" ")
    tampered = _run_cli(
        output_root,
        "inspect",
        result["run_id"],
        "--output-root",
        str(output_root),
    )
    assert tampered.returncode == 1
    assert "checksum failed" in tampered.stderr


def test_checked_in_runtime_configuration_and_model_lock_are_strict() -> None:
    config = load_runtime_config(ROOT / "ses.json")
    lock = load_model_lock(ROOT / config.models_lock)

    assert lock.engine_version == "2.1.220"
    assert lock.roles[ModelRole.MAIN].model_id == "deepseek-ai/DeepSeek-V3.2"
    assert lock.roles[ModelRole.SIMULATOR].model_id.startswith("Qwen/")


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {
                "preflight_passed": False,
                "exit_status": None,
                "grade_status": None,
            },
            RunOutcome.EXPECT_FAIL,
        ),
        (
            {
                "preflight_passed": True,
                "exit_status": EngineExitStatus.SUCCESS,
                "grade_status": GradeStatus.FAIL,
            },
            RunOutcome.AGENT_FAIL,
        ),
        (
            {
                "preflight_passed": True,
                "exit_status": EngineExitStatus.SUCCESS,
                "grade_status": GradeStatus.ERROR,
            },
            RunOutcome.JUDGE_ERROR,
        ),
        (
            {
                "preflight_passed": True,
                "exit_status": EngineExitStatus.ERROR,
                "grade_status": None,
            },
            RunOutcome.INFRASTRUCTURE_ERROR,
        ),
        (
            {
                "preflight_passed": True,
                "exit_status": EngineExitStatus.BUDGET_STOP,
                "grade_status": None,
            },
            RunOutcome.BUDGET_STOP,
        ),
        (
            {
                "preflight_passed": True,
                "exit_status": EngineExitStatus.SUCCESS,
                "grade_status": GradeStatus.PASS,
            },
            RunOutcome.PASS,
        ),
    ],
)
def test_run_outcome_categories_have_explicit_precedence(
    arguments: dict[str, object], expected: RunOutcome
) -> None:
    assert classify_run_outcome(**arguments) is expected  # type: ignore[arg-type]


def test_a_run_id_cannot_overwrite_existing_artifacts(tmp_path: Path) -> None:
    completed = run_pinned_case(tmp_path, run_id="run-fixed-integration")
    original = completed.result_path.read_bytes()

    with pytest.raises(SingleCaseRunError) as exc_info:
        run_pinned_case(tmp_path, run_id="run-fixed-integration")

    assert exc_info.value.outcome is RunOutcome.INFRASTRUCTURE_ERROR
    assert completed.result_path.read_bytes() == original
