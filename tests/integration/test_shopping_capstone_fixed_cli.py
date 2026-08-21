from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from ses.contracts import (
    AcceptedSkillReleaseManifest,
    AutoEvolveState,
    AutoLoopStatus,
    CapstoneFinalReceipt,
    CapstoneIndex,
    FinalAggregateReport,
    GateDecision,
    GateOutcome,
    PairedComparison,
    RegistryEventType,
    SchemaVersion,
    TriggerEvalResult,
)
from ses.contracts.shopping import ShoppingPairMetrics, ShoppingScenario
from ses.shopping.course_workflow import ShoppingLearnerReceipt
from ses.shopping.profile import load_shopping_profile
from ses.shopping.registry import open_shopping_registry
from ses.skills.installer import normalized_skill_sha256

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPSTONE_ROOT = PROJECT_ROOT / "fixtures" / "seed" / "capstone-shopping-assistant"
PROFILE = CAPSTONE_ROOT / "profiles" / "fixed-v1.json"


def _offline_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        normalized = name.upper()
        if "API_KEY" in normalized or "TOKEN" in normalized:
            environment.pop(name)
    environment.update(
        {
            "ALL_PROXY": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
        }
    )
    return environment


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ses.cli.app", *args],
        cwd=PROJECT_ROOT,
        env=_offline_environment(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _run_json(*args: str) -> dict[str, object]:
    completed = _run(*args, "--json")
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def _base(experiment: Path) -> tuple[str, ...]:
    return (
        "--profile",
        str(PROFILE),
        "--experiment-root",
        str(experiment),
    )


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _assert_no_reference_fallback(paths: Sequence[Path]) -> None:
    for path in paths:
        assert b"reference_fallback" not in path.read_bytes(), path


def test_fixed_capstone_cli_runs_each_learner_stage_to_accepted_install(
    tmp_path: Path,
) -> None:
    experiment = (tmp_path / "experiment").resolve()
    registry_root = experiment / "registry"
    loaded_profile = load_shopping_profile(PROFILE)
    assert not experiment.exists()
    assert sum(loaded_profile.profile.source_group_counts.values()) == 10
    assert sum(loaded_profile.profile.episode_slot_counts.values()) == 40
    assert loaded_profile.profile.episode_slot_counts == {
        "creator": 8,
        "develop": 12,
        "selection": 8,
        "final": 12,
    }

    created = _run_json("skill", "create-v0", *_base(experiment))
    create_receipt = ShoppingLearnerReceipt.model_validate_json(
        (experiment / "receipts/create.json").read_bytes()
    )
    assert created["stage"] == "create"
    assert created["primary_metrics"] == {
        "creator_seed_count": 8,
        "seed_review_status": "course_original_reviewed",
    }
    assert create_receipt.source_kind == "learner_created"
    assert {reference.path for reference in create_receipt.inputs} == {
        "inputs/creator-projections.json"
    }
    learner_skill_sha256 = normalized_skill_sha256(experiment / "skill/v0")
    assert create_receipt.skill_sha256 == learner_skill_sha256

    static = _run_json("skill", "static-gate", *_base(experiment))
    static_receipt = ShoppingLearnerReceipt.model_validate_json(
        (experiment / "receipts/static.json").read_bytes()
    )
    assert static["primary_metrics"] == {"static_gate": "pass"}
    assert static_receipt.skill_sha256 == learner_skill_sha256

    triggered = _run_json("trigger-eval", *_base(experiment))
    trigger_receipt = ShoppingLearnerReceipt.model_validate_json(
        (experiment / "receipts/trigger.json").read_bytes()
    )
    trigger_result = TriggerEvalResult.model_validate_json(
        (experiment / "trigger-eval.json").read_bytes()
    )
    assert triggered["primary_metrics"] == {
        "negative_pass_count": 10,
        "positive_pass_count": 10,
        "precision": 1.0,
        "recall": 1.0,
    }
    assert trigger_result.tp == trigger_result.tn == 10
    assert trigger_receipt.skill_sha256 == learner_skill_sha256

    paired = _run_json("paired-comparison", *_base(experiment))
    paired_receipt = ShoppingLearnerReceipt.model_validate_json(
        (experiment / "receipts/paired.json").read_bytes()
    )
    comparison = PairedComparison.model_validate_json(
        (experiment / "paired-comparison.json").read_bytes()
    )
    pair_metrics = ShoppingPairMetrics.model_validate_json(
        (experiment / "shopping-pair-metrics.json").read_bytes()
    )
    assert _mapping(paired["primary_metrics"])["paired_case_count"] == 12
    assert comparison.schema_version is SchemaVersion.V1ALPHA2
    assert comparison.baseline_run_id != comparison.skill_run_id
    assert len(comparison.cases) == pair_metrics.comparable_case_count == 12
    assert {row.scenario for row in pair_metrics.strata} == set(ShoppingScenario)
    assert {row.case_count for row in pair_metrics.strata} == {3}
    assert paired_receipt.skill_sha256 == learner_skill_sha256
    first_trace = comparison.cases[0].skill_trace
    assert first_trace is not None
    _run_json(
        "inspect",
        "paired-trace",
        str(experiment / first_trace.path),
        *_base(experiment),
    )

    evolved = _run_json("evolve", *_base(experiment))
    assert evolved["stage"] == "evolve"
    patch_operation_count = _mapping(evolved["primary_metrics"])[
        "patch_operation_count"
    ]
    assert isinstance(patch_operation_count, int)
    assert 1 <= patch_operation_count <= 3
    _run_json(
        "inspect",
        "failure-evidence",
        str(experiment / "failure-evidence.json"),
        *_base(experiment),
    )
    _run_json(
        "inspect",
        "failure-card",
        str(experiment / "manual-evolution/failure-cards.json"),
        *_base(experiment),
    )

    initialized = _run_json(
        "registry",
        "init",
        *_base(experiment),
        "--registry",
        str(registry_root),
        "--initial-skill",
        str(experiment / "skill/v0"),
        "--initial-evidence",
        str(experiment / "v0-pipeline-summary.json"),
    )
    assert initialized["event_type"] == RegistryEventType.INITIALIZED.value
    registered = _run_json(
        "registry",
        "register",
        *_base(experiment),
        "--registry",
        str(registry_root),
        "--candidate",
        str(experiment / "manual-evolution"),
    )
    assert registered["event_type"] == RegistryEventType.CANDIDATE_REGISTERED.value

    manual_gate = _run_json("gate", "candidate", *_base(experiment))
    manual_decision_path = (
        registry_root / "gates/gate-shopping-manual/gate-decision.json"
    )
    manual_decision = GateDecision.model_validate_json(
        manual_decision_path.read_bytes()
    )
    assert manual_gate["outcome"] == GateOutcome.ACCEPTED.value
    assert manual_decision.schema_version is SchemaVersion.V1ALPHA2
    assert manual_decision.outcome is GateOutcome.ACCEPTED
    promoted = _run_json(
        "registry",
        "promote",
        *_base(experiment),
        "--registry",
        str(registry_root),
        "--candidate-id",
        manual_decision.candidate_id,
        "--gate-decision",
        str(manual_decision_path),
    )
    assert promoted["event_type"] == RegistryEventType.PROMOTED.value
    assert open_shopping_registry(registry_root).audit().current_accepted_sha256 == (
        manual_decision.candidate_skill_sha256
    )

    automated = _run_json("auto-evolve", *_base(experiment))
    state_path = experiment / "state.json"
    state = AutoEvolveState.model_validate_json(state_path.read_bytes())
    assert _mapping(automated["primary_metrics"])["completed_rounds"] == 2
    assert state.status is AutoLoopStatus.STOPPED
    assert [row.gate_outcome for row in state.rounds] == [
        GateOutcome.ACCEPTED,
        GateOutcome.REJECTED,
    ]
    assert state.final_report is None
    assert not (experiment / "final").exists()
    registry = open_shopping_registry(registry_root)
    assert registry.audit().current_accepted_sha256 == (
        state.current_accepted_skill_sha256
    )

    rejected_decision_path = experiment / state.rounds[1].gate_decision.path
    rejected_decision = GateDecision.model_validate_json(
        rejected_decision_path.read_bytes()
    )
    assert rejected_decision.outcome is GateOutcome.REJECTED
    _run_json(
        "inspect",
        "gate-decision",
        str(rejected_decision_path),
        *_base(experiment),
    )
    _run_json(
        "inspect",
        "registry-history",
        str(registry_root / "events.jsonl"),
        *_base(experiment),
    )
    assert {path.stem for path in (experiment / "reviews").glob("*.json")} == {
        "failure_card",
        "failure_evidence",
        "gate_decision",
        "paired_trace",
        "registry_history",
    }

    first_final = _run_json("final", *_base(experiment))
    final_receipt_path = experiment / "final/capstone-final-receipt.json"
    final_receipt = CapstoneFinalReceipt.model_validate_json(
        final_receipt_path.read_bytes()
    )
    final_report = FinalAggregateReport.model_validate_json(
        (experiment / "final/final-aggregate.json").read_bytes()
    )
    assert _mapping(first_final["primary_metrics"])["safety_violation_count"] == 0
    assert final_receipt.subject_skill_sha256 == state.current_accepted_skill_sha256
    assert final_receipt.safety_violation_count == 0
    assert final_report.case_count == 12
    assert final_report.safety_violation_count == 0
    assert final_report.scenario_metrics is not None
    assert [row.case_count for row in final_report.scenario_metrics] == [3, 3, 3, 3]
    final_snapshot = _tree_snapshot(experiment / "final")
    state_after_first_final = state_path.read_bytes()

    second_final = _run_json("final", *_base(experiment))
    assert second_final == first_final
    assert _tree_snapshot(experiment / "final") == final_snapshot
    assert state_path.read_bytes() == state_after_first_final

    l3_path = experiment / "l3.html"
    l3 = _run_json(
        "l3-render",
        *_base(experiment),
        "--output",
        str(l3_path),
    )
    assert l3["stage"] == "l3_render"
    l3_text = l3_path.read_text(encoding="utf-8")
    assert "Final full success" in l3_text
    assert "Scenario final aggregates" in l3_text

    portfolio_root = experiment / "portfolio"
    portfolio = _run_json(
        "portfolio-export",
        *_base(experiment),
        "--output",
        str(portfolio_root),
    )
    assert portfolio["stage"] == "portfolio_export"
    assert (portfolio_root / "manifest.json").is_file()

    package_root = experiment / "package"
    packaged = _run_json(
        "skill",
        "package",
        *_base(experiment),
        "--registry",
        str(registry_root),
        "--current-accepted",
        "--output",
        str(package_root),
    )
    release_manifest_path = package_root / "release-manifest.json"
    release = AcceptedSkillReleaseManifest.model_validate_json(
        release_manifest_path.read_bytes()
    )
    assert packaged["accepted_skill_sha256"] == state.current_accepted_skill_sha256
    assert release.accepted_skill_sha256 == state.current_accepted_skill_sha256

    index_path = experiment / "capstone-index.json"
    indexed = _run_json(
        "capstone-index",
        *_base(experiment),
        "--output",
        str(index_path),
    )
    index = CapstoneIndex.model_validate_json(index_path.read_bytes())
    assert _mapping(indexed["primary_metrics"])["learning_completion"] == (
        "workflow_complete"
    )
    assert index.learning_completion == "workflow_complete"
    assert index.current_accepted_skill_sha256 == state.current_accepted_skill_sha256
    assert index.profile_sha256 == loaded_profile.profile_sha256
    assert len(index.review_receipts) == 5

    install_root = experiment / "installed"
    installed = _run_json(
        "skill-install",
        "--accepted-package",
        str(release_manifest_path),
        *_base(experiment),
        "--destination",
        str(install_root),
    )
    assert installed["source_kind"] == "registry_accepted"
    assert installed["sha256"] == state.current_accepted_skill_sha256
    installed_destination = Path(str(installed["destination"]))
    assert installed_destination == install_root / "shopping-assistant"
    assert (installed_destination / "SKILL.md").is_file()

    _assert_no_reference_fallback(
        (
            *(experiment / "receipts").glob("*.json"),
            *(experiment / "reviews").glob("*.json"),
            experiment / "failure-evidence.json",
            experiment / "manual-evolution/failure-cards.json",
            experiment / "manual-evolution/patch.json",
            state_path,
            final_receipt_path,
            l3_path,
            portfolio_root / "manifest.json",
            release_manifest_path,
            package_root / "skill/skill-manifest.json",
            index_path,
        )
    )
