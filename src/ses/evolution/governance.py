"""Application workflow for gating and recording one Skill candidate."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ses.contracts import CandidateArtifact, GateDecision, GatePolicy, content_sha256
from ses.evolution.gate import (
    GateEvaluationAdapter,
    GateRequest,
    default_gate_policy,
    run_candidate_gate,
)
from ses.evolution.registry import SkillRegistry
from ses.skills.static_gate import (
    DEFAULT_STATIC_GATE_POLICY,
    StaticGatePolicy,
    run_static_gate,
)


@dataclass(frozen=True, slots=True)
class CandidateGovernanceCommand:
    """Inputs for one gate run and its append-only Registry transition."""

    registry_root: Path
    candidate_bundle: Path
    selection_lock: Path
    project_root: Path
    gate_id: str
    command_id: str
    mode: Literal["fixed", "live"]
    measured_at: datetime
    policy: GatePolicy | None = None
    static_gate_policy: StaticGatePolicy = DEFAULT_STATIC_GATE_POLICY


def govern_candidate(
    command: CandidateGovernanceCommand,
    *,
    adapter: GateEvaluationAdapter,
) -> GateDecision:
    """Gate the candidate against the current parent and record the decision."""

    policy = command.policy
    if policy is None:
        if command.mode == "live":
            raise ValueError("live governance requires an explicit policy")
        policy = default_gate_policy(command.project_root, command.selection_lock)

    registry = SkillRegistry(
        command.registry_root,
        initial_static_gate=lambda source: run_static_gate(
            source,
            policy=command.static_gate_policy,
        ),
    )
    state = registry.audit()
    decision_path = registry.root / "gates" / command.gate_id / "gate-decision.json"
    if decision_path.is_file() and not decision_path.is_symlink():
        try:
            decision = GateDecision.model_validate_json(
                decision_path.read_text(encoding="utf-8")
            )
            candidate_bytes = (command.candidate_bundle / "candidate.json").read_bytes()
            candidate = CandidateArtifact.model_validate_json(candidate_bytes)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("existing gate decision or candidate is invalid") from exc
        measured_at = command.measured_at
        if measured_at.tzinfo is None or measured_at.utcoffset() is None:
            raise ValueError("gate measured_at must include a timezone")
        if (
            decision.gate_id != command.gate_id
            or decision.lineage_id != state.lineage_id
            or decision.mode != command.mode
            or decision.decided_at != measured_at.astimezone(UTC)
            or decision.gate_policy_sha256 != content_sha256(policy)
            or candidate.candidate_id != decision.candidate_id
            or candidate.content_sha256 != decision.candidate_skill_sha256
            or candidate.parent_skill_sha256 != decision.accepted_skill_sha256
            or hashlib.sha256(candidate_bytes).hexdigest() != decision.candidate.sha256
        ):
            raise ValueError("existing gate decision does not match the command")
        registry.record_decision(
            command_id=command.command_id,
            decision_path=decision_path,
            occurred_at=command.measured_at,
        )
        return decision

    decision = run_candidate_gate(
        GateRequest(
            gate_id=command.gate_id,
            lineage_id=state.lineage_id,
            workspace_root=registry.root,
            accepted_skill=registry.version_path(state.current_accepted_sha256),
            candidate_bundle=command.candidate_bundle,
            selection_lock=command.selection_lock,
            policy=policy,
            mode=command.mode,
            measured_at=command.measured_at,
            static_gate_policy=command.static_gate_policy,
        ),
        adapter=adapter,
    )
    registry.record_decision(
        command_id=command.command_id,
        decision_path=decision_path,
        occurred_at=command.measured_at,
    )
    return decision


__all__ = ["CandidateGovernanceCommand", "govern_candidate"]
