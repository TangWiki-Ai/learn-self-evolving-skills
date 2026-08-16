from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from ses.contracts import Trace
from ses.evaluator import run_pinned_case
from ses.skills.comparison import SkillDemoComparison
from ses.skills.creator import FakeCreator
from ses.skills.demo import run_skill_demo
from ses.skills.demo_engine import OfflineSkillDemoEngine
from ses.skills.installer import normalized_skill_sha256, write_skill_manifest
from ses.skills.selection import CandidateMode

ROOT = Path(__file__).parents[2]
CASE_ID = "state-bench-customer-support-2-return-defective-electronics"


def _run_cli(output_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ses.cli.skill_demo", *arguments],
        cwd=output_root.parent,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _unrelated_skill(tmp_path: Path) -> Path:
    source = tmp_path / "unrelated"
    source.mkdir()
    (source / "SKILL.md").write_text(
        """---
name: issue-triage
description: Apply only when sorting software bug reports.
version: unrelated-v1
---
# Issue triage
Inspect the issue, preview labels, confirm priority, and verify the final board.
This Skill does not apply to customer returns or order tools.
""",
        encoding="utf-8",
    )
    write_skill_manifest(
        source,
        name="issue-triage",
        version="unrelated-v1",
        files=("SKILL.md",),
    )
    return source


def test_same_offline_engine_changes_behavior_from_the_installed_skill(
    tmp_path: Path,
) -> None:
    result = run_skill_demo(tmp_path / "demo", mode=CandidateMode.GENERATE)
    comparison = SkillDemoComparison.model_validate_json(
        (result.output_root / result.comparison_artifact).read_bytes()
    )

    assert comparison.case_id == CASE_ID
    assert comparison.measured is True
    assert comparison.protocol.same_for_both_runs is True
    assert comparison.protocol.engine == "offline-workspace-skill-engine-v1"
    assert comparison.skill.source == "generated"

    without_skill = comparison.runs.without_skill
    with_skill = comparison.runs.with_skill
    assert without_skill["run_id"] != with_skill["run_id"]
    assert without_skill["outcome"] == "agent_fail"
    assert with_skill["outcome"] == "pass"
    assert without_skill["skill"] == {"version": None, "sha256": None}
    assert _mapping(with_skill["skill"])["version"] == "demo-v1"
    assert not _mapping(without_skill["state_diff"])["changed"]
    assert _mapping(with_skill["state_diff"])["changed"]

    without_trace = Trace.model_validate_json(
        result.baseline_run.run_dir.joinpath("trace.json").read_bytes()
    )
    with_trace = Trace.model_validate_json(
        result.with_skill_run.run_dir.joinpath("trace.json").read_bytes()
    )
    assert without_trace.trace_id != with_trace.trace_id
    assert without_trace.request.prompt == with_trace.request.prompt
    assert without_trace.request.allowed_tools == with_trace.request.allowed_tools
    assert without_trace.request.timeout_seconds == with_trace.request.timeout_seconds
    assert without_trace.skill_version is None
    assert with_trace.skill_version == "demo-v1"

    without_workspace = next(
        result.baseline_run.run_dir.joinpath("workspaces").glob("case-*/workspace")
    )
    with_workspace = next(
        result.with_skill_run.run_dir.joinpath("workspaces").glob("case-*/workspace")
    )
    assert without_workspace != with_workspace
    assert not without_workspace.joinpath(".claude", "skills").exists()
    installed = with_workspace / ".claude" / "skills" / "return-support-demo"
    assert sorted(
        path.relative_to(installed).as_posix()
        for path in installed.rglob("*")
        if path.is_file()
    ) == ["SKILL.md", "references/return-checklist.md"]


def test_installing_an_unrelated_skill_does_not_make_the_case_pass(
    tmp_path: Path,
) -> None:
    source = _unrelated_skill(tmp_path)

    completed = run_pinned_case(
        tmp_path / "runs",
        engine_factory=OfflineSkillDemoEngine,
        skill_source=source,
        skill_version="unrelated-v1",
        skill_sha256=normalized_skill_sha256(source),
    )

    assert completed.outcome.value == "agent_fail"


def test_cli_accepts_candidate_or_explicit_reference(tmp_path: Path) -> None:
    candidate = FakeCreator().create(tmp_path / "candidate", seed_traces=()).source
    candidate_run = _run_cli(
        tmp_path / "candidate-demo",
        "--output-root",
        str(tmp_path / "candidate-demo"),
        "--candidate",
        str(candidate),
        "--json",
    )
    reference_run = _run_cli(
        tmp_path / "reference-demo",
        "--output-root",
        str(tmp_path / "reference-demo"),
        "--reference",
        "--json",
    )

    assert candidate_run.returncode == 0, candidate_run.stderr
    assert reference_run.returncode == 0, reference_run.stderr
    assert json.loads(candidate_run.stdout)["skill"]["source"] == "candidate"
    assert json.loads(reference_run.stdout)["skill"]["source"] == "reference"


def test_fallback_reason_is_persisted_for_creator_failure(tmp_path: Path) -> None:
    result = run_skill_demo(
        tmp_path / "demo",
        mode=CandidateMode.GENERATE,
        creator=FakeCreator(failure="offline creator failure"),
    )

    comparison = SkillDemoComparison.model_validate_json(
        (result.output_root / result.comparison_artifact).read_bytes()
    )
    assert comparison.skill.source == "reference_fallback"
    assert comparison.skill.fallback_reason is not None
    assert comparison.skill.fallback_reason.startswith("uninstallable:")


def test_checked_in_and_runtime_comparisons_share_one_strict_schema(
    tmp_path: Path,
) -> None:
    checked_in = SkillDemoComparison.model_validate_json(
        ROOT.joinpath(
            "course/ch01-see-the-difference/comparison-artifact.json"
        ).read_bytes()
    )
    runtime = run_skill_demo(tmp_path / "demo", mode=CandidateMode.REFERENCE)
    current = SkillDemoComparison.model_validate_json(
        (runtime.output_root / runtime.comparison_artifact).read_bytes()
    )

    assert checked_in.schema_version == current.schema_version == "v1alpha1"
    assert checked_in.record_type == current.record_type
    assert checked_in.measured is False
    assert current.measured is True
    assert checked_in.source.kind == "checked_in_reference"
    assert current.source.kind == "current_run"
