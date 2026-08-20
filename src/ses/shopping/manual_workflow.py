"""Learner-visible evidence, candidate, Gate, and Registry stages."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from ses.automation.capstone import write_opaque_split_locks
from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    EvolutionPipelineSummary,
    GateDecision,
    GateOutcome,
    PairedComparison,
    RegistryEvent,
)
from ses.evolution.diagnosis import SHOPPING_DIAGNOSIS_POLICY
from ses.evolution.governance import CandidateGovernanceCommand, govern_candidate
from ses.evolution.updater import SHOPPING_UPDATER_POLICY, FixedShoppingUpdater
from ses.evolution.workflow import run_evolution_workflow
from ses.shopping.course_workflow import (
    SHOPPING_STATIC_GATE_POLICY,
    ShoppingLearnerReceipt,
)
from ses.shopping.evidence import (
    FIXED_DEVELOP_REVIEWS,
    export_shopping_failure_evidence,
)
from ses.shopping.gate import (
    FixedShoppingEpisodeGateAdapter,
    FixedShoppingGateAdapter,
    ShoppingGateScenario,
    shopping_gate_policy,
)
from ses.shopping.profile import LoadedShoppingProfile, shopping_experiment_id
from ses.shopping.registry import open_shopping_registry
from ses.skills.installer import normalized_skill_sha256

_FIXED_TIME = datetime(2026, 8, 20, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ShoppingEvolutionStageResult:
    summary: EvolutionPipelineSummary
    evidence_path: Path
    failure_cards_path: Path
    patch_path: Path
    candidate_bundle: Path


@dataclass(frozen=True, slots=True)
class ShoppingGateStageResult:
    decision: GateDecision
    decision_path: Path
    registry_event: RegistryEvent


def _resolve(root: Path, reference: ArtifactRef) -> Path:
    if reference.root is not ArtifactRoot.RUN:
        raise ValueError("shopping learner receipt must use its experiment root")
    path = root / reference.path
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("shopping learner artifact escapes its experiment") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError("shopping learner artifact must be a regular file")
    reference.verify_bytes(path.read_bytes())
    return path


def _paired_from_receipt(
    profile: LoadedShoppingProfile,
    *,
    experiment_root: Path,
    receipt_path: Path,
) -> tuple[ShoppingLearnerReceipt, Path, PairedComparison]:
    receipt = ShoppingLearnerReceipt.model_validate_json(receipt_path.read_bytes())
    if (
        receipt.stage != "paired"
        or receipt.profile_sha256 != profile.profile_sha256
        or receipt.network_used
        or receipt.source_kind != "learner_created"
    ):
        raise ValueError("manual evolve requires the matching learner paired receipt")
    comparison: PairedComparison | None = None
    comparison_path: Path | None = None
    for reference in receipt.outputs:
        path = _resolve(experiment_root, reference)
        try:
            candidate = PairedComparison.model_validate_json(path.read_bytes())
        except ValueError:
            continue
        if comparison is not None:
            raise ValueError("paired receipt contains multiple comparisons")
        comparison = candidate
        comparison_path = path
    if comparison is None or comparison_path is None:
        raise ValueError("paired receipt does not bind a canonical comparison")
    if comparison.skill_sha256 != receipt.skill_sha256:
        raise ValueError("paired receipt Skill differs from its comparison")
    return receipt, comparison_path, comparison


def run_shopping_evolution_stage(
    *,
    profile: LoadedShoppingProfile,
    experiment_root: Path,
    paired_receipt: Path,
) -> ShoppingEvolutionStageResult:
    """Project reviewed develop failures and publish one bounded candidate."""

    if profile.profile.mode != "fixed":
        raise ValueError("offline shopping evolution requires the fixed profile")
    receipt, comparison_path, _ = _paired_from_receipt(
        profile,
        experiment_root=experiment_root,
        receipt_path=paired_receipt,
    )
    parent = experiment_root / "skill" / "v0"
    if normalized_skill_sha256(parent) != receipt.skill_sha256:
        raise ValueError("learner v0 changed after its paired evaluation")
    evidence_path = experiment_root / "failure-evidence.json"
    export_shopping_failure_evidence(
        experiment_root=experiment_root,
        comparison_path=comparison_path,
        output_path=evidence_path,
        reviewed_subcodes=FIXED_DEVELOP_REVIEWS,
        expected_skill_sha256=receipt.skill_sha256,
    )
    bundle = experiment_root / "manual-evolution"
    summary = run_evolution_workflow(
        parent_dir=parent,
        evidence_path=evidence_path,
        output_root=bundle,
        updater=FixedShoppingUpdater(),
        mode="fixed",
        workspace_root=experiment_root / "updater-workspaces",
        diagnosis_policy=SHOPPING_DIAGNOSIS_POLICY,
        updater_policy=SHOPPING_UPDATER_POLICY,
        static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
    )
    return ShoppingEvolutionStageResult(
        summary=summary,
        evidence_path=evidence_path,
        failure_cards_path=bundle / "failure-cards.json",
        patch_path=bundle / "patch.json",
        candidate_bundle=bundle,
    )


def register_shopping_candidate(
    *,
    registry_root: Path,
    candidate_bundle: Path,
    command_id: str = "command-shopping-manual-register",
    occurred_at: datetime = _FIXED_TIME,
) -> RegistryEvent:
    return open_shopping_registry(registry_root).register_candidate(
        command_id=command_id,
        candidate_bundle=candidate_bundle,
        occurred_at=occurred_at,
    )


def run_shopping_gate_stage(
    *,
    profile: LoadedShoppingProfile,
    experiment_root: Path,
    registry_root: Path,
    candidate_bundle: Path,
    scenario: ShoppingGateScenario = "accept",
    gate_id: str = "gate-shopping-manual",
    command_id: str = "command-shopping-manual-gate",
    measured_at: datetime = _FIXED_TIME,
) -> ShoppingGateStageResult:
    """Run the shared eight-stage Gate and append its Registry decision."""

    experiment_id = shopping_experiment_id(profile)
    locks = write_opaque_split_locks(
        experiment_root=experiment_root,
        experiment_id=experiment_id,
        profile_sha256=profile.profile_sha256,
        mode=profile.profile.mode,
        selection_case_count=profile.profile.episode_slot_counts["selection"],
        selection_commitment_sha256=(
            profile.profile.protected_split_commitments["selection"]
        ),
        final_commitment_sha256=profile.profile.protected_split_commitments["final"],
        generated_at=measured_at,
    )
    registry = open_shopping_registry(registry_root)
    before = registry.audit()
    adapter = (
        FixedShoppingEpisodeGateAdapter(
            profile=profile,
            experiment_root=experiment_root,
            selection_lock=locks.selection,
            accepted_skill_source=registry.version_path(before.current_accepted_sha256),
            candidate_skill_source=candidate_bundle / "skill",
            scenario=cast(Literal["accept", "tie", "unauthorized"], scenario),
        )
        if scenario in {"accept", "tie", "unauthorized"}
        else FixedShoppingGateAdapter(scenario)
    )
    decision = govern_candidate(
        CandidateGovernanceCommand(
            registry_root=registry_root,
            candidate_bundle=candidate_bundle,
            selection_lock=locks.selection,
            project_root=experiment_root,
            gate_id=gate_id,
            command_id=command_id,
            mode="fixed",
            measured_at=measured_at,
            policy=shopping_gate_policy(
                profile,
                selection_lock=locks.selection,
                experiment_id=experiment_id,
            ),
            static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
        ),
        adapter=adapter,
    )
    after = open_shopping_registry(registry_root).audit()
    if len(after.events) != len(before.events) + 1:
        raise ValueError("manual Gate did not append exactly one Registry decision")
    decision_path = registry_root / "gates" / gate_id / "gate-decision.json"
    decision_reference = after.events[-1].gate_decision
    if decision_reference is None or (
        hashlib.sha256(decision_path.read_bytes()).hexdigest()
        != decision_reference.sha256
    ):
        raise ValueError("Registry decision reference differs from Gate output")
    return ShoppingGateStageResult(
        decision=decision,
        decision_path=decision_path,
        registry_event=after.events[-1],
    )


def promote_shopping_candidate(
    *,
    registry_root: Path,
    decision_path: Path,
    candidate_id: str,
    command_id: str = "command-shopping-manual-promote",
    occurred_at: datetime = _FIXED_TIME,
) -> RegistryEvent:
    """Promote only the candidate named by an accepted canonical decision."""

    decision = GateDecision.model_validate_json(decision_path.read_bytes())
    if decision.outcome is not GateOutcome.ACCEPTED:
        raise ValueError("a rejected shopping candidate has no promote branch")
    if decision.candidate_id != candidate_id:
        raise ValueError("promote target differs from the accepted GateDecision")
    return open_shopping_registry(registry_root).promote(
        command_id=command_id,
        candidate_id=candidate_id,
        occurred_at=occurred_at,
    )


__all__ = [
    "ShoppingEvolutionStageResult",
    "ShoppingGateStageResult",
    "promote_shopping_candidate",
    "register_shopping_candidate",
    "run_shopping_evolution_stage",
    "run_shopping_gate_stage",
]
