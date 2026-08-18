from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ses.contracts import (
    CandidateArtifact,
    GateOutcome,
    RegistryEventType,
    artifact_json_bytes,
    content_sha256,
)
from ses.evolution.gate import FixedGateAdapter, default_gate_policy
from ses.evolution.governance import CandidateGovernanceCommand, govern_candidate
from ses.evolution.registry import SkillRegistry
from ses.evolution.updater import FakeUpdater
from ses.evolution.workflow import run_evolution_workflow

ROOT = Path(__file__).parents[2]
PARENT = ROOT / "course/ch07-create-v0/artifacts/skill/v0"
SEED_EVIDENCE = ROOT / "course/ch07-create-v0/artifacts/summary.json"
FAILURE_EVIDENCE = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
SELECTION_LOCK = ROOT / "data/testset/protected/selection-manifest.json"
NOW = datetime(2026, 8, 18, 9, tzinfo=UTC)


def test_govern_candidate_runs_the_gate_and_records_its_decision(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=FAILURE_EVIDENCE,
        output_root=candidate,
        updater=FakeUpdater(),
        mode="fixed",
    )
    registry = SkillRegistry(tmp_path / "registry")
    initialized = registry.initialize(
        command_id="command-governance-initialize",
        accepted_skill=PARENT,
        evidence_paths=(SEED_EVIDENCE,),
        occurred_at=NOW,
    )
    registered = registry.register_candidate(
        command_id="command-governance-register",
        candidate_bundle=candidate,
        occurred_at=NOW,
    )
    policy = default_gate_policy(ROOT, SELECTION_LOCK).model_copy(
        update={"policy_id": "gate-policy-application-override"}
    )

    decision = govern_candidate(
        CandidateGovernanceCommand(
            registry_root=registry.root,
            candidate_bundle=candidate,
            selection_lock=SELECTION_LOCK,
            project_root=ROOT,
            gate_id="gate-application-service",
            command_id="command-governance-gate",
            mode="fixed",
            measured_at=NOW,
            policy=policy,
        ),
        adapter=FixedGateAdapter(),
    )

    state = registry.audit()
    governed = state.versions[registered.version_sha256]
    assert decision.outcome is GateOutcome.ACCEPTED
    assert decision.accepted_skill_sha256 == initialized.version_sha256
    assert decision.gate_policy_sha256 == content_sha256(policy)
    assert state.events[-1].event_type is RegistryEventType.CANDIDATE_ACCEPTED
    assert governed.gate_decision is not None
    recorded_decision = state.events[-1].gate_decision
    assert recorded_decision is not None
    assert governed.gate_decision.sha256 == recorded_decision.sha256


def test_live_governance_requires_an_explicit_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="live governance requires an explicit policy"):
        govern_candidate(
            CandidateGovernanceCommand(
                registry_root=tmp_path / "registry",
                candidate_bundle=tmp_path / "candidate",
                selection_lock=SELECTION_LOCK,
                project_root=ROOT,
                gate_id="gate-live-without-policy",
                command_id="command-live-without-policy",
                mode="live",
                measured_at=NOW,
            ),
            adapter=FixedGateAdapter(),
        )

    assert not (tmp_path / "registry").exists()


def test_govern_candidate_retries_a_persisted_decision_after_append_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=FAILURE_EVIDENCE,
        output_root=candidate,
        updater=FakeUpdater(),
        mode="fixed",
    )
    registry = SkillRegistry(tmp_path / "registry")
    registry.initialize(
        command_id="command-retry-initialize",
        accepted_skill=PARENT,
        evidence_paths=(SEED_EVIDENCE,),
        occurred_at=NOW,
    )
    registry.register_candidate(
        command_id="command-retry-register",
        candidate_bundle=candidate,
        occurred_at=NOW,
    )
    command = CandidateGovernanceCommand(
        registry_root=registry.root,
        candidate_bundle=candidate,
        selection_lock=SELECTION_LOCK,
        project_root=ROOT,
        gate_id="gate-retry-after-append-failure",
        command_id="command-retry-gate",
        mode="fixed",
        measured_at=NOW,
    )
    original = SkillRegistry.record_decision
    attempts = 0

    def fail_once(self: SkillRegistry, **kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated append outage")
        return original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(SkillRegistry, "record_decision", fail_once)

    with pytest.raises(OSError, match="append outage"):
        govern_candidate(command, adapter=FixedGateAdapter())

    decision_path = (
        registry.root / "gates/gate-retry-after-append-failure/gate-decision.json"
    )
    assert decision_path.is_file()
    before = registry.events_path.read_bytes()
    mismatched_candidate = tmp_path / "mismatched-candidate"
    shutil.copytree(candidate, mismatched_candidate)
    candidate_path = mismatched_candidate / "candidate.json"
    candidate_record = CandidateArtifact.model_validate_json(
        candidate_path.read_bytes()
    ).model_copy(update={"candidate_id": "candidate-recovery-mismatch"})
    candidate_path.write_bytes(artifact_json_bytes(candidate_record))

    with pytest.raises(ValueError, match="does not match the command"):
        govern_candidate(
            replace(command, candidate_bundle=mismatched_candidate),
            adapter=FixedGateAdapter(),
        )

    assert registry.events_path.read_bytes() == before

    decision = govern_candidate(command, adapter=FixedGateAdapter())

    assert decision.outcome is GateOutcome.ACCEPTED
    assert attempts == 2
    assert registry.events_path.read_bytes().startswith(before)
    assert (
        registry.audit().events[-1].event_type is RegistryEventType.CANDIDATE_ACCEPTED
    )
