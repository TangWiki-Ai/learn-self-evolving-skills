# ruff: noqa: RUF001 -- Assertions intentionally match learner-facing Chinese copy.
from __future__ import annotations

import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path

from ses.contracts.runner import RunnerStatus
from ses.journey.course import run_station_5, run_station_6
from ses.runner import (
    BaselineRunner,
    BudgetLimits,
    CaseEvaluation,
    develop_catalog_sha256,
    load_develop_catalog,
)
from ses.runner.baseline import EvaluationContext
from ses.skills.installer import (
    load_skill_manifest,
    normalized_skill_sha256,
    write_skill_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEED_SKILL = PROJECT_ROOT / "fixtures/seed/skill/v0"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _prepare_candidate(workspace: Path) -> tuple[Path, str, str]:
    journey_root = workspace / ".ses"
    parent = journey_root / "versions/v0"
    shutil.copytree(SEED_SKILL, parent)
    parent_hash = normalized_skill_sha256(parent)
    candidate = journey_root / "candidates/candidate-a"
    shutil.copytree(SEED_SKILL, candidate)
    manifest = load_skill_manifest(candidate)
    skill_path = candidate / "SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8").rstrip()
        + "\n\nUse the preview amount exactly when confirming.\n",
        encoding="utf-8",
    )
    (candidate / "skill-manifest.json").unlink()
    write_skill_manifest(
        candidate,
        name=manifest.name,
        version="candidate",
        files=tuple(item.path for item in manifest.files),
        source_version="journey-station5-test",
        provider_compatibility=manifest.provider_compatibility,
    )
    candidate_hash = normalized_skill_sha256(candidate)
    _write_json(
        journey_root / "current-candidate.json",
        {
            "candidate_path": candidate.relative_to(workspace).as_posix(),
            "candidate_skill_sha256": candidate_hash,
            "parent_skill_sha256": parent_hash,
        },
    )
    return candidate, candidate_hash, parent_hash


def _prepare_baseline_and_decisions(workspace: Path) -> tuple[str, tuple[str, ...]]:
    journey_root = workspace / ".ses"
    case_ids = tuple(load_develop_catalog(mode="fixed"))
    target = case_ids[0]
    baseline_path = journey_root / "evidence/v0-baseline-report.json"
    _write_json(
        baseline_path,
        {
            "cases": [
                {
                    "case_id": case_id,
                    "first_status": (
                        RunnerStatus.AGENT_FAIL.value
                        if case_id == target
                        else RunnerStatus.PASS.value
                    ),
                }
                for case_id in case_ids
            ]
        },
    )
    _write_json(
        journey_root / "evidence/station-0.json",
        {"artifact_paths": [baseline_path.relative_to(workspace).as_posix()]},
    )
    _write_json(
        journey_root / "decisions/station-1-selection.json",
        {"selected_case_ids": [target]},
    )
    _write_json(
        journey_root / "decisions/station-2-attributions.json",
        {"labels": [{"case_id": target, "label": "skill:knowledge"}]},
    )
    return target, case_ids


def _seed_recorded_run(
    workspace: Path,
    *,
    run_id: str,
    case_ids: tuple[str, ...],
    candidate_hash: str,
    status: RunnerStatus,
) -> None:
    catalog = load_develop_catalog(mode="fixed")

    def evaluate(case_id: str, iteration_id: str, max_turns: int) -> CaseEvaluation:
        del max_turns
        return CaseEvaluation(
            case_id=case_id,
            iteration_id=iteration_id,
            status=status,
            turn_count=1,
            input_tokens=1,
            output_tokens=1,
            cost_amount=Decimal("0.001"),
            cost_currency="CNY",
        )

    class TestEvaluator:
        def evaluate_attempt(self, context: EvaluationContext) -> CaseEvaluation:
            return evaluate(context.case_id, context.iteration_id, context.max_turns)

    BaselineRunner(workspace / ".ses/runs", TestEvaluator()).run(
        run_id=run_id,
        case_ids=case_ids,
        iterations=1,
        budgets=BudgetLimits(
            max_cases=len(case_ids),
            max_turns_per_case=3,
            cost_currency="CNY",
        ),
        data_version=develop_catalog_sha256(catalog),
        model_lock_hash=hashlib.sha256(
            (PROJECT_ROOT / "models.lock.json").read_bytes()
        ).hexdigest(),
        skill_hash=candidate_hash,
        protocol_version="ses-eight-step-journey-v1",
    )


def test_station5_reports_door2_as_not_run_when_a_real_target_replay_fails(
    tmp_path: Path,
) -> None:
    _candidate, candidate_hash, _parent_hash = _prepare_candidate(tmp_path)
    target, case_ids = _prepare_baseline_and_decisions(tmp_path)
    _seed_recorded_run(
        tmp_path,
        run_id=f"run-journey-station5-target-{candidate_hash[:12]}-fixed",
        case_ids=(target,),
        candidate_hash=candidate_hash,
        status=RunnerStatus.AGENT_FAIL,
    )

    result = run_station_5(
        workspace=tmp_path,
        project_root=PROJECT_ROOT,
        mode="fixed",
        timeout=30,
        decision="refine",
    )

    assert result.status == "needs_attention"
    assert result.metrics["full_regression_ran"] is False
    assert result.metrics["candidate_pass_count"] == 0
    assert result.metrics["regression_case_count"] == 0
    assert result.metrics["expected_regression_case_count"] == len(case_ids)
    gate_path = next(path for path in result.artifacts if path.name.startswith("gate-"))
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate["full_regression_ran"] is False
    assert gate["candidate_pass_count"] == 0
    assert gate["regression_case_count"] == 0
    assert gate["doors"]["1_target_replay"]["statuses"] == {
        target: RunnerStatus.AGENT_FAIL.value
    }
    report = (tmp_path / ".ses/reports/station-5-gate.html").read_text(encoding="utf-8")
    assert target in report
    assert RunnerStatus.AGENT_FAIL.value in report
    assert "<h2>未运行</h2>" in report
    assert "门 1 没有全部通过，因此门 2 尚未运行" in report
    assert "没有 Skill 目标" not in report


def test_station5_records_complete_gate_metrics_that_station6_can_bind(
    tmp_path: Path,
) -> None:
    _candidate, candidate_hash, parent_hash = _prepare_candidate(tmp_path)
    target, case_ids = _prepare_baseline_and_decisions(tmp_path)
    _seed_recorded_run(
        tmp_path,
        run_id=f"run-journey-station5-target-{candidate_hash[:12]}-fixed",
        case_ids=(target,),
        candidate_hash=candidate_hash,
        status=RunnerStatus.PASS,
    )
    _seed_recorded_run(
        tmp_path,
        run_id=f"run-journey-station5-regression-{candidate_hash[:12]}-fixed",
        case_ids=case_ids,
        candidate_hash=candidate_hash,
        status=RunnerStatus.PASS,
    )

    result = run_station_5(
        workspace=tmp_path,
        project_root=PROJECT_ROOT,
        mode="fixed",
        timeout=30,
        decision="follow-gate",
    )

    assert result.status == "completed"
    assert result.metrics["full_regression_ran"] is True
    assert result.metrics["regression_case_set_complete"] is True
    assert result.metrics["regression_case_count"] == len(case_ids)
    assert result.metrics["candidate_pass_count"] == len(case_ids)
    assert result.metrics["target_regression_pass_count"] == 1
    gate_path = next(path for path in result.artifacts if path.name.startswith("gate-"))
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    assert gate["outcome"] == "accepted"
    assert gate["candidate_changed"] is True
    assert gate["candidate_skill_sha256"] == candidate_hash
    assert gate["parent_skill_sha256"] == parent_hash
    assert gate["doors"]["2_full_regression"] == {
        "candidate_pass_count": len(case_ids),
        "case_set_complete": True,
        "expected_case_count": len(case_ids),
        "pass_to_fail_count": 0,
        "passed": True,
        "ran": True,
        "regression_case_count": len(case_ids),
        "target_pass_count": 1,
    }
    report = (tmp_path / ".ses/reports/station-5-gate.html").read_text(encoding="utf-8")
    assert f"实际回归 {len(case_ids)}/{len(case_ids)} 条" in report

    release = run_station_6(workspace=tmp_path, action="release")
    assert release.status == "completed"
