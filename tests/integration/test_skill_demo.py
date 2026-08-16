from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from ses.cli.skill_demo import run_skill_demo
from ses.contracts import Trace
from ses.skills.creator import FakeCreator

ROOT = Path(__file__).parents[2]
CASE_ID = "state-bench-customer-support-2-return-defective-electronics"


def _run_cli(output_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ses.cli.skill_demo", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def test_skill_demo_writes_two_fresh_qualitative_runs(tmp_path: Path) -> None:
    output_root = tmp_path / "demo"
    completed = _run_cli(output_root, "--output-root", str(output_root), "--json")

    assert completed.returncode == 0, completed.stderr
    comparison = json.loads(completed.stdout)
    assert comparison["case_id"] == CASE_ID
    assert comparison["claim"] == "qualitative_demo_only"
    assert comparison["protocol"]["same_for_both_runs"] is True
    assert comparison["model_config"]["same_for_both_runs"] is True
    assert comparison["skill"]["source"] == "generated"

    without_skill = comparison["runs"]["without_skill"]
    with_skill = comparison["runs"]["with_skill"]
    assert without_skill["run_id"] != with_skill["run_id"]
    assert without_skill["outcome"] == "agent_fail"
    assert with_skill["outcome"] == "pass"
    assert without_skill["skill"] == {"version": None, "sha256": None}
    assert with_skill["skill"]["version"] == "demo-v1"
    assert len(with_skill["skill"]["sha256"]) == 64
    assert without_skill["messages"]
    assert with_skill["messages"]
    assert without_skill["tool_calls"]
    assert with_skill["tool_calls"]
    assert not without_skill["state_diff"]["changed"]
    assert with_skill["state_diff"]["changed"]

    without_workspace = next(
        (output_root / without_skill["run_id"] / "workspaces").glob("case-*/workspace")
    )
    with_workspace = next(
        (output_root / with_skill["run_id"] / "workspaces").glob("case-*/workspace")
    )
    assert without_workspace != with_workspace
    assert not (without_workspace / ".claude" / "skills").exists()
    installed = with_workspace / ".claude" / "skills" / "return-support-demo"
    assert sorted(
        path.relative_to(installed).as_posix()
        for path in installed.rglob("*")
        if path.is_file()
    ) == ["SKILL.md", "references/return-checklist.md"]

    without_artifacts = _mapping(without_skill["artifacts"])
    with_artifacts = _mapping(with_skill["artifacts"])
    without_trace_ref = _mapping(without_artifacts["trace"])
    with_trace_ref = _mapping(with_artifacts["trace"])
    without_trace = Trace.model_validate_json(
        (
            output_root / without_skill["run_id"] / str(without_trace_ref["path"])
        ).read_bytes()
    )
    with_trace = Trace.model_validate_json(
        (output_root / with_skill["run_id"] / str(with_trace_ref["path"])).read_bytes()
    )
    assert without_trace.trace_id != with_trace.trace_id
    assert without_trace.request.prompt == with_trace.request.prompt
    assert without_trace.request.allowed_tools == with_trace.request.allowed_tools
    assert without_trace.request.timeout_seconds == with_trace.request.timeout_seconds
    assert without_trace.skill_version is None
    assert with_trace.skill_version == "demo-v1"
    assert with_trace.skill_sha256 == with_skill["skill"]["sha256"]

    comparison_path = output_root / comparison["comparison_artifact"]
    assert comparison_path.is_file()
    assert "qualitative" in comparison_path.read_text(encoding="utf-8")


def test_weak_creator_uses_explicit_reference_skill_fallback(tmp_path: Path) -> None:
    result = run_skill_demo(
        tmp_path / "demo",
        creator=FakeCreator(failure="offline creator failure"),
    )

    assert result.skill_source == "reference_fallback"
    assert result.fallback_reason == "offline creator failure"
    comparison = json.loads(
        (result.output_root / result.comparison_artifact).read_text(encoding="utf-8")
    )
    assert comparison["skill"]["source"] == "reference_fallback"
    assert comparison["skill"]["reference"] is True
    assert comparison["runs"]["with_skill"]["skill"]["version"] == "reference-v1"
