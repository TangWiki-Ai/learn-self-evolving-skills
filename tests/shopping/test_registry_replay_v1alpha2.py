from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ses.automation.capstone import OpaqueSplitLockPaths, write_opaque_split_locks
from ses.contracts import (
    GateDecision,
    GateOutcome,
    GatePolicy,
    GateReason,
    GateStep,
    GateStepStatus,
    SchemaVersion,
    SelectionPairEvaluation,
    VersionStatus,
    artifact_json_bytes,
)
from ses.evolution.gate import GateRequest, run_candidate_gate
from ses.evolution.registry import RegistryError, SkillRegistry
from ses.shopping.course_workflow import (
    SHOPPING_STATIC_GATE_POLICY,
    run_shopping_create_stage,
    run_shopping_paired_stage,
    run_shopping_static_stage,
    run_shopping_trigger_stage,
)
from ses.shopping.fixed_course import build_fixed_develop_evaluation
from ses.shopping.gate import (
    FixedShoppingGateAdapter,
    shopping_gate_policy,
)
from ses.shopping.manual_workflow import (
    ShoppingEvolutionStageResult,
    run_shopping_evolution_stage,
)
from ses.shopping.profile import (
    LoadedShoppingProfile,
    load_shopping_profile,
    shopping_experiment_id,
)
from ses.shopping.registry import open_shopping_registry

MEASURED_AT = datetime(2026, 8, 20, 12, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPSTONE_ROOT = PROJECT_ROOT / "course" / "capstone-shopping-assistant"


def _registered_shopping_candidate(
    tmp_path: Path,
) -> tuple[
    LoadedShoppingProfile,
    ShoppingEvolutionStageResult,
    SkillRegistry,
    str,
    OpaqueSplitLockPaths,
    GatePolicy,
]:
    profile = load_shopping_profile(CAPSTONE_ROOT / "profiles/fixed-v1.json")
    experiment_root = (tmp_path / "shopping-pipeline").resolve()
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE_ROOT / "fixtures/creator-projections",
        experiment_root=experiment_root,
    )
    static = run_shopping_static_stage(
        profile=profile,
        experiment_root=experiment_root,
        skill_source=created.skill_source,
        create_receipt=created.receipt_path,
    )
    trigger = run_shopping_trigger_stage(
        profile=profile,
        experiment_root=experiment_root,
        skill_source=created.skill_source,
        static_receipt=static.receipt_path,
    )
    fixed = build_fixed_develop_evaluation(
        profile,
        learner_skill_sha256=created.receipt.skill_sha256,
        learner_skill_source=created.skill_source,
    )
    paired = run_shopping_paired_stage(
        profile=profile,
        experiment_root=experiment_root,
        skill_source=created.skill_source,
        trigger_receipt=trigger.receipt_path,
        tasks=fixed.tasks,
        baseline_evaluator=fixed.baseline_evaluator,
        skill_evaluator=fixed.skill_evaluator,
    )
    evolved = run_shopping_evolution_stage(
        profile=profile,
        experiment_root=experiment_root,
        paired_receipt=paired.receipt_path,
    )
    registry = open_shopping_registry(experiment_root / "registry")
    initialized = registry.initialize(
        command_id="command-shopping-replay-initialize",
        accepted_skill=created.skill_source,
        evidence_paths=(experiment_root / "v0-pipeline-summary.json",),
        occurred_at=MEASURED_AT,
        lineage_id=f"lineage-shopping-fixed-{profile.profile_sha256[:16]}",
    )
    registry.register_candidate(
        command_id="command-shopping-replay-register",
        candidate_bundle=evolved.candidate_bundle,
        occurred_at=MEASURED_AT + timedelta(seconds=1),
    )
    experiment_id = shopping_experiment_id(profile)
    locks = write_opaque_split_locks(
        experiment_root=experiment_root,
        experiment_id=experiment_id,
        profile_sha256=profile.profile_sha256,
        mode="fixed",
        selection_case_count=8,
        selection_commitment_sha256=(
            profile.profile.protected_split_commitments["selection"]
        ),
        final_commitment_sha256=(profile.profile.protected_split_commitments["final"]),
        generated_at=MEASURED_AT,
    )
    policy = shopping_gate_policy(
        profile,
        selection_lock=locks.selection,
        experiment_id=experiment_id,
    )
    return profile, evolved, registry, initialized.version_sha256, locks, policy


def test_registry_rejects_a_rehashed_shopping_decision_forged_against_its_pair(
    tmp_path: Path,
) -> None:
    _, evolved, registry, accepted_sha256, locks, policy = (
        _registered_shopping_candidate(tmp_path)
    )
    gate_id = "gate-shopping-replay-forgery"
    decision = run_candidate_gate(
        GateRequest(
            gate_id=gate_id,
            lineage_id=registry.audit().lineage_id,
            workspace_root=registry.root,
            accepted_skill=registry.version_path(accepted_sha256),
            candidate_bundle=evolved.candidate_bundle,
            selection_lock=locks.selection,
            policy=policy,
            static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
            mode="fixed",
            measured_at=MEASURED_AT + timedelta(seconds=2),
        ),
        adapter=FixedShoppingGateAdapter("unauthorized"),
    )
    selection = decision.steps[3]
    pair_path = registry.root / selection.evidence[0].path
    pair_bytes = pair_path.read_bytes()
    pair = SelectionPairEvaluation.model_validate_json(pair_bytes)
    assert decision.schema_version is SchemaVersion.V1ALPHA2
    assert decision.reason_codes == (GateReason.SAFETY_VIOLATION,)
    assert sum(row.candidate_safety_violation_count or 0 for row in pair.cases) == 1

    pair_ref = selection.evidence[0]
    forged_steps = (
        *decision.steps[:4],
        *(
            GateStep(
                stage=step.stage,
                status=GateStepStatus.PASS,
                evidence=(pair_ref,),
            )
            for step in decision.steps[4:]
        ),
    )
    forged = GateDecision.model_validate(
        {
            **decision.model_dump(mode="python"),
            "steps": forged_steps,
            "metrics": decision.metrics.model_copy(
                update={
                    "candidate_mean_strict_reward": Decimal("1"),
                    "candidate_safety_violation_count": 0,
                }
            ),
            "outcome": GateOutcome.ACCEPTED,
            "reason_codes": (GateReason.ACCEPTED,),
        }
    )
    forged_path = registry.root / f"gates/{gate_id}/forged-decision.json"
    forged_path.write_bytes(artifact_json_bytes(forged))
    before = registry.events_path.read_bytes()

    with pytest.raises(RegistryError, match="shopping aggregate metrics"):
        registry.record_decision(
            command_id="command-shopping-replay-forged-decision",
            decision_path=forged_path,
            occurred_at=MEASURED_AT + timedelta(seconds=3),
        )

    assert pair_path.read_bytes() == pair_bytes
    assert hashlib.sha256(pair_path.read_bytes()).hexdigest() == pair_ref.sha256
    assert registry.events_path.read_bytes() == before


def test_registry_replays_a_canonical_shopping_safety_rejection(
    tmp_path: Path,
) -> None:
    _, evolved, registry, accepted_sha256, locks, policy = (
        _registered_shopping_candidate(tmp_path)
    )
    gate_id = "gate-shopping-replay-safety"
    decision = run_candidate_gate(
        GateRequest(
            gate_id=gate_id,
            lineage_id=registry.audit().lineage_id,
            workspace_root=registry.root,
            accepted_skill=registry.version_path(accepted_sha256),
            candidate_bundle=evolved.candidate_bundle,
            selection_lock=locks.selection,
            policy=policy,
            static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
            mode="fixed",
            measured_at=MEASURED_AT + timedelta(seconds=2),
        ),
        adapter=FixedShoppingGateAdapter("unauthorized"),
    )

    registry.record_decision(
        command_id="command-shopping-replay-safety",
        decision_path=registry.root / f"gates/{gate_id}/gate-decision.json",
        occurred_at=MEASURED_AT + timedelta(seconds=3),
    )

    state = open_shopping_registry(registry.root).audit()
    assert decision.reason_codes == (GateReason.SAFETY_VIOLATION,)
    assert (
        state.versions[decision.candidate_skill_sha256].status is VersionStatus.REJECTED
    )
    assert state.current_accepted_sha256 == accepted_sha256


def test_registry_replays_a_canonical_shopping_strict_reward_rejection(
    tmp_path: Path,
) -> None:
    _, evolved, registry, accepted_sha256, locks, policy = (
        _registered_shopping_candidate(tmp_path)
    )
    gate_id = "gate-shopping-replay-strict"
    decision = run_candidate_gate(
        GateRequest(
            gate_id=gate_id,
            lineage_id=registry.audit().lineage_id,
            workspace_root=registry.root,
            accepted_skill=registry.version_path(accepted_sha256),
            candidate_bundle=evolved.candidate_bundle,
            selection_lock=locks.selection,
            policy=policy,
            static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
            mode="fixed",
            measured_at=MEASURED_AT + timedelta(seconds=2),
        ),
        adapter=FixedShoppingGateAdapter("strict-regression"),
    )

    registry.record_decision(
        command_id="command-shopping-replay-strict",
        decision_path=registry.root / f"gates/{gate_id}/gate-decision.json",
        occurred_at=MEASURED_AT + timedelta(seconds=3),
    )

    state = open_shopping_registry(registry.root).audit()
    assert decision.reason_codes == (GateReason.STRICT_REGRESSION,)
    assert (
        state.versions[decision.candidate_skill_sha256].status is VersionStatus.REJECTED
    )
    assert state.current_accepted_sha256 == accepted_sha256


def test_registry_promotes_and_replays_a_canonical_shopping_acceptance(
    tmp_path: Path,
) -> None:
    _, evolved, registry, accepted_sha256, locks, policy = (
        _registered_shopping_candidate(tmp_path)
    )
    gate_id = "gate-shopping-replay-accepted"
    decision = run_candidate_gate(
        GateRequest(
            gate_id=gate_id,
            lineage_id=registry.audit().lineage_id,
            workspace_root=registry.root,
            accepted_skill=registry.version_path(accepted_sha256),
            candidate_bundle=evolved.candidate_bundle,
            selection_lock=locks.selection,
            policy=policy,
            static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
            mode="fixed",
            measured_at=MEASURED_AT + timedelta(seconds=2),
        ),
        adapter=FixedShoppingGateAdapter("accept"),
    )
    registry.record_decision(
        command_id="command-shopping-replay-accept",
        decision_path=registry.root / f"gates/{gate_id}/gate-decision.json",
        occurred_at=MEASURED_AT + timedelta(seconds=3),
    )
    registry.promote(
        command_id="command-shopping-replay-promote",
        candidate_id=decision.candidate_id,
        occurred_at=MEASURED_AT + timedelta(seconds=4),
    )

    state = open_shopping_registry(registry.root).audit()
    assert decision.outcome is GateOutcome.ACCEPTED
    assert state.current_accepted_sha256 == decision.candidate_skill_sha256
    assert state.versions[decision.candidate_skill_sha256].verified is True
