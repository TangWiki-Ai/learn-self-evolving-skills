"""Lesson 9 solution delegates every decision to production governance modules."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from ses.contracts import GateDecision, GateOutcome
from ses.evolution.gate import (
    FixedGateAdapter,
    FixedGateScenario,
    GateRequest,
    default_gate_policy,
    run_candidate_gate,
)
from ses.evolution.registry import RegistryState, SkillRegistry


class GovernanceResult:
    """Observable result of the complete fixed/offline lesson workflow."""

    __slots__ = (
        "candidate_skill_sha256",
        "decision",
        "initial_skill_sha256",
        "state",
    )

    def __init__(
        self,
        *,
        decision: GateDecision,
        state: RegistryState,
        initial_skill_sha256: str,
        candidate_skill_sha256: str,
    ) -> None:
        self.decision = decision
        self.state = state
        self.initial_skill_sha256 = initial_skill_sha256
        self.candidate_skill_sha256 = candidate_skill_sha256


def gate_candidate(
    *,
    project_root: Path,
    governance_root: Path,
    accepted_skill: Path,
    candidate_bundle: Path,
    selection_lock: Path,
    lineage_id: str,
    scenario: FixedGateScenario,
    gate_id: str,
    measured_at: datetime,
) -> GateDecision:
    """Run the production Gate with an explicitly synthetic offline adapter."""

    return run_candidate_gate(
        GateRequest(
            gate_id=gate_id,
            lineage_id=lineage_id,
            workspace_root=governance_root,
            accepted_skill=accepted_skill,
            candidate_bundle=candidate_bundle,
            selection_lock=selection_lock,
            policy=default_gate_policy(project_root, selection_lock),
            mode="fixed",
            measured_at=measured_at,
        ),
        adapter=FixedGateAdapter(scenario),
    )


def record_and_maybe_promote(
    *,
    registry: SkillRegistry,
    decision: GateDecision,
    decision_path: Path,
    occurred_at: datetime,
) -> RegistryState:
    """Record every terminal decision, then promote only an accepted candidate."""

    registry.record_decision(
        command_id="command-lesson-record-decision",
        decision_path=decision_path,
        occurred_at=occurred_at,
    )
    if decision.outcome is GateOutcome.ACCEPTED:
        registry.promote(
            command_id="command-lesson-promote",
            candidate_id=decision.candidate_id,
            occurred_at=occurred_at + timedelta(seconds=1),
        )
    return registry.audit()


def rollback(
    *,
    registry: SkillRegistry,
    target_skill_sha256: str,
    occurred_at: datetime,
) -> RegistryState:
    """Append a rollback to an existing, previously verified version."""

    registry.rollback(
        command_id="command-lesson-rollback",
        target_skill_sha256=target_skill_sha256,
        occurred_at=occurred_at,
    )
    return registry.audit()


def govern(
    *,
    project_root: Path,
    governance_root: Path,
    accepted_skill: Path,
    initial_evidence: Path,
    candidate_bundle: Path,
    selection_lock: Path,
    scenario: FixedGateScenario,
    gate_id: str,
    occurred_at: datetime,
    rollback_after_promote: bool = False,
) -> GovernanceResult:
    """Run registration, Gate, decision, optional promotion, and optional rollback."""

    registry = SkillRegistry(governance_root)
    initialized = registry.initialize(
        command_id="command-lesson-initialize",
        accepted_skill=accepted_skill,
        evidence_paths=(initial_evidence,),
        occurred_at=occurred_at,
    )
    registered = registry.register_candidate(
        command_id="command-lesson-register",
        candidate_bundle=candidate_bundle,
        occurred_at=occurred_at + timedelta(seconds=1),
    )
    decision = gate_candidate(
        project_root=project_root,
        governance_root=governance_root,
        accepted_skill=accepted_skill,
        candidate_bundle=candidate_bundle,
        selection_lock=selection_lock,
        lineage_id=registry.audit().lineage_id,
        scenario=scenario,
        gate_id=gate_id,
        measured_at=occurred_at + timedelta(seconds=2),
    )
    state = record_and_maybe_promote(
        registry=registry,
        decision=decision,
        decision_path=governance_root / "gates" / gate_id / "gate-decision.json",
        occurred_at=occurred_at + timedelta(seconds=3),
    )
    if rollback_after_promote:
        state = rollback(
            registry=registry,
            target_skill_sha256=initialized.version_sha256,
            occurred_at=occurred_at + timedelta(seconds=5),
        )
    return GovernanceResult(
        decision=decision,
        state=state,
        initial_skill_sha256=initialized.version_sha256,
        candidate_skill_sha256=registered.version_sha256,
    )
