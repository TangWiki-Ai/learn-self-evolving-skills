from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ses.skills.paired import PairCategory, compare_run_events, run_fresh_paired

ROOT = Path(__file__).parents[2]


def _v0(tmp_path: Path) -> Path:
    return Path(
        shutil.copytree(
            ROOT / "course" / "ch07-create-v0" / "artifacts" / "skill" / "v0",
            tmp_path / "v0",
        )
    )


def test_paired_comparison_runs_fresh_isolated_sides_with_skill_as_only_variable(
    tmp_path: Path,
) -> None:
    comparison = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "paired",
        project_root=ROOT,
    )

    assert len(comparison.cases) == 15
    assert {row.category for row in comparison.cases} == {PairCategory.BOTH_PASS}
    assert comparison.baseline_run_id != comparison.skill_run_id
    assert comparison.baseline_events.sha256
    assert comparison.skill_events.sha256
    assert comparison.pair_execution_sha256
    assert comparison.category_counts[PairCategory.FAIL_TO_PASS] == 0
    assert comparison.category_counts[PairCategory.PASS_TO_FAIL] == 0
    assert comparison.category_counts[PairCategory.BOTH_FAIL] == 0
    assert comparison.category_counts[PairCategory.BOTH_PASS] == 15
    assert all(row.baseline_status == row.skill_status for row in comparison.cases)
    assert comparison.skill_input_tokens == comparison.baseline_input_tokens
    assert comparison.skill_cost_amount == comparison.baseline_cost_amount
    assert all(row.baseline_trace and row.skill_trace for row in comparison.cases)
    assert all(
        row.baseline_state_diff and row.skill_state_diff for row in comparison.cases
    )
    assert all(row.baseline_grade and row.skill_grade for row in comparison.cases)
    workspaces = list((tmp_path / "paired").glob("run-*/workspaces/case-*/workspace"))
    assert len(workspaces) == 30


def test_paired_comparison_rejects_protocol_mismatch(tmp_path: Path) -> None:
    comparison = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "paired",
        project_root=ROOT,
    )
    baseline_events = tmp_path / "paired" / comparison.baseline_run_id / "events.jsonl"
    skill_events = tmp_path / "paired" / comparison.skill_run_id / "events.jsonl"
    lines = skill_events.read_text(encoding="utf-8").splitlines()
    started = json.loads(lines[0])
    started["config"]["protocol_version"] = "incompatible-v2"
    lines[0] = json.dumps(started, sort_keys=True, separators=(",", ":"))
    skill_events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="protocol"):
        compare_run_events(
            baseline_events,
            skill_events,
            output_root=tmp_path / "paired",
            measurement_kind=comparison.measurement_kind,
            measured_at=comparison.measured_at,
            engine_version=comparison.engine_version,
            model_id=comparison.model_id,
        )


def test_paired_comparison_preserves_partial_infrastructure_evidence(
    tmp_path: Path,
) -> None:
    comparison = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "paired",
        project_root=ROOT,
    )
    baseline_events = tmp_path / "paired" / comparison.baseline_run_id / "events.jsonl"
    skill_events = tmp_path / "paired" / comparison.skill_run_id / "events.jsonl"
    lines = baseline_events.read_text(encoding="utf-8").splitlines()
    attempt = json.loads(lines[1])
    attempt["status"] = "infrastructure_error"
    attempt["artifacts"]["traces"] = []
    attempt["artifacts"]["state_diff"] = None
    attempt["artifacts"]["grade"] = None
    attempt["error"] = "provider timeout"
    lines[1] = json.dumps(attempt, sort_keys=True, separators=(",", ":"))
    baseline_events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rebuilt = compare_run_events(
        baseline_events,
        skill_events,
        output_root=tmp_path / "paired",
        measurement_kind=comparison.measurement_kind,
        measured_at=comparison.measured_at,
        engine_version=comparison.engine_version,
        model_id=comparison.model_id,
    )

    row = rebuilt.cases[0]
    assert row.baseline_status.value == "infrastructure_error"
    assert row.baseline_trace is None
    assert row.baseline_state_diff is None
    assert row.baseline_grade is None


def test_fixed_paired_comparison_is_byte_reproducible(tmp_path: Path) -> None:
    first = run_fresh_paired(
        skill_source=_v0(tmp_path),
        output_root=tmp_path / "first",
        project_root=ROOT,
    )
    second = run_fresh_paired(
        skill_source=tmp_path / "v0",
        output_root=tmp_path / "second",
        project_root=ROOT,
    )

    assert first == second
    assert (tmp_path / "first" / first.baseline_events.path).read_bytes() == (
        tmp_path / "second" / second.baseline_events.path
    ).read_bytes()
    assert (tmp_path / "first" / first.skill_events.path).read_bytes() == (
        tmp_path / "second" / second.skill_events.path
    ).read_bytes()
