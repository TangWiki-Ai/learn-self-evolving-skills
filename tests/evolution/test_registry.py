from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    CandidateArtifact,
    GateDecision,
    GateOutcome,
    GatePolicy,
    GateStage,
    Patch,
    RegistryEvent,
    SchemaVersion,
    SelectionPairEvaluation,
    TriggerEvalResult,
    UpdatePatchOperation,
    VersionStatus,
    artifact_json_bytes,
)
from ses.evolution.candidate import load_runtime_files
from ses.evolution.gate import (
    FixedGateAdapter,
    FixedGateScenario,
    GateRequest,
    default_gate_policy,
    run_candidate_gate,
)
from ses.evolution.registry import RegistryError, SkillRegistry
from ses.evolution.updater import FakeUpdater, UpdaterRequest
from ses.evolution.workflow import run_evolution_workflow
from ses.skills.installer import (
    load_skill_manifest,
    normalized_skill_sha256,
    write_skill_manifest,
)

ROOT = Path(__file__).parents[2]
PARENT = ROOT / "course/ch07-create-v0/artifacts/skill/v0"
SEED_EVIDENCE = ROOT / "course/ch07-create-v0/artifacts/summary.json"
FAILURE_EVIDENCE = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
SELECTION_LOCK = ROOT / "data/testset/protected/selection-manifest.json"
NOW = datetime(2026, 8, 18, 9, tzinfo=UTC)


class _VariantUpdater(FakeUpdater):
    def propose(self, request: UpdaterRequest) -> Patch:
        original = super().propose(request)
        operations = tuple(
            operation.model_copy(
                update={
                    "content": operation.content
                    + "\nPrefer the narrowest eligible item when the request is ambiguous.\n"
                }
            )
            if operation.operation == "update"
            else operation
            for operation in original.operations
        )
        return Patch(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="skill_patch",
            patch_id="patch-test-variant",
            parent_skill_sha256=original.parent_skill_sha256,
            operations=operations,
        )


def _candidate(tmp_path: Path) -> Path:
    bundle = tmp_path / "candidate"
    run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=FAILURE_EVIDENCE,
        output_root=bundle,
        updater=FakeUpdater(),
        mode="fixed",
    )
    return bundle


def _variant_candidate(tmp_path: Path) -> Path:
    bundle = tmp_path / "variant-candidate"
    run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=FAILURE_EVIDENCE,
        output_root=bundle,
        updater=_VariantUpdater(),
        mode="fixed",
    )
    return bundle


def _second_generation_candidate(
    tmp_path: Path,
    *,
    parent: Path,
    evidence_candidate: Path,
) -> Path:
    bundle = tmp_path / "candidate-generation-two"
    skill = bundle / "skill"
    parent_hash = normalized_skill_sha256(parent)
    parent_manifest = load_skill_manifest(parent)
    files = load_runtime_files(parent)
    original = files["SKILL.md"]
    files["SKILL.md"] = (
        original + "\nPrefer the previously verified branch for ambiguous requests.\n"
    )
    source_candidate = CandidateArtifact.model_validate_json(
        (evidence_candidate / "candidate.json").read_text(encoding="utf-8")
    )
    source_operation = source_candidate.patch.operations[0]
    operation = UpdatePatchOperation(
        operation="update",
        target="SKILL.md",
        precondition_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
        content=files["SKILL.md"],
        trace_evidence=source_operation.trace_evidence,
        assertion_evidence=source_operation.assertion_evidence,
        reason="Exercise a second immutable lineage edge.",
        risk="The extra instruction may be too narrow.",
        failure_card_ids=source_operation.failure_card_ids,
    )
    patch = Patch(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_patch",
        patch_id="patch-test-generation-two",
        parent_skill_sha256=parent_hash,
        operations=(operation,),
    )
    for relative, content in files.items():
        target = skill / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    version = f"candidate-{patch.patch_sha256[:12]}"
    write_skill_manifest(
        skill,
        name=parent_manifest.name,
        version=version,
        files=tuple(sorted(files)),
        source_version=f"parent:{parent_hash}",
        provider_compatibility=parent_manifest.provider_compatibility,
    )
    manifest = load_skill_manifest(skill)
    content_hash = normalized_skill_sha256(skill)
    candidate = CandidateArtifact(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_candidate",
        candidate_id=f"candidate-{patch.patch_sha256[:12]}",
        parent_skill_sha256=parent_hash,
        patch_sha256=patch.patch_sha256,
        content_sha256=content_hash,
        version=version,
        static_gate_status="pass",
        patch=patch,
        files=files,
        manifest=manifest,
        creation_protocol="evidence-linked-patch-v1",
    )
    (bundle / "candidate.json").write_bytes(artifact_json_bytes(candidate))
    return bundle


def _decision(
    root: Path,
    candidate: Path,
    scenario: FixedGateScenario,
    gate_id: str,
) -> GateDecision:
    return run_candidate_gate(
        GateRequest(
            gate_id=gate_id,
            lineage_id=SkillRegistry(root).audit().lineage_id,
            workspace_root=root,
            accepted_skill=PARENT,
            candidate_bundle=candidate,
            selection_lock=SELECTION_LOCK,
            policy=default_gate_policy(ROOT, SELECTION_LOCK),
            mode="fixed",
            measured_at=NOW,
        ),
        adapter=FixedGateAdapter(scenario),
    )


def _workspace_ref(root: Path, path: Path) -> ArtifactRef:
    content = path.read_bytes()
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _trigger_prompt_hash(result: TriggerEvalResult) -> str:
    payload = [
        {
            "prompt_id": row.prompt_id,
            "prompt": row.prompt,
            "expected_trigger": row.expected_trigger,
        }
        for row in result.prompts
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _forged_trigger_decision(
    root: Path,
    decision: GateDecision,
    *,
    forgery: str,
) -> GateDecision:
    trigger_step = next(
        step for step in decision.steps if step.stage is GateStage.TRIGGER
    )
    trigger_path = root / trigger_step.evidence[0].path
    trigger = TriggerEvalResult.model_validate_json(
        trigger_path.read_text(encoding="utf-8")
    )
    if forgery == "one-row":
        positive = next(row for row in trigger.prompts if row.expected_trigger)
        forged_trigger = trigger.model_copy(
            update={
                "prompts": (positive,),
                "tp": 1,
                "fp": 0,
                "tn": 0,
                "fn": 0,
                "precision": 1.0,
                "recall": 1.0,
            }
        )
        forged_trigger = forged_trigger.model_copy(
            update={"prompt_set_sha256": _trigger_prompt_hash(forged_trigger)}
        )
    elif forgery == "wrong-prompt":
        first = trigger.prompts[0].model_copy(update={"prompt": "forged prompt"})
        forged_trigger = trigger.model_copy(
            update={"prompts": (first, *trigger.prompts[1:])}
        )
        forged_trigger = forged_trigger.model_copy(
            update={"prompt_set_sha256": _trigger_prompt_hash(forged_trigger)}
        )
    elif forgery == "wrong-model":
        forged_trigger = trigger.model_copy(update={"model_id": "unlocked-model"})
    elif forgery == "wrong-hash":
        forged_trigger = trigger.model_copy(update={"prompt_set_sha256": "f" * 64})
    elif forgery == "high-cost":
        forged_trigger = trigger.model_copy(
            update={
                "usage": trigger.usage.model_copy(
                    update={
                        "cost_amount": Decimal("100"),
                        "cost_currency": "USD",
                    }
                )
            }
        )
    else:
        raise AssertionError(f"unsupported Trigger forgery: {forgery}")
    forged_path = trigger_path.with_name(f"forged-trigger-{forgery}.json")
    forged_path.write_bytes(artifact_json_bytes(forged_trigger))
    forged_ref = _workspace_ref(root, forged_path)
    steps = tuple(
        step.model_copy(update={"evidence": (forged_ref,)})
        if step.stage is GateStage.TRIGGER
        else step
        for step in decision.steps
    )
    return decision.model_copy(update={"steps": steps})


def _forged_pair_decision(
    root: Path,
    decision: GateDecision,
    *,
    measured_at: datetime | None = None,
    evaluation_nonce: str | None = None,
    cost_currency: str | None = None,
    accepted_run_id: str | None = None,
    candidate_run_id: str | None = None,
) -> GateDecision:
    selection = next(
        step for step in decision.steps if step.stage is GateStage.SELECTION
    )
    pair_path = root / selection.evidence[0].path
    pair = SelectionPairEvaluation.model_validate_json(
        pair_path.read_text(encoding="utf-8")
    )
    log_refs: dict[str, ArtifactRef] = {}
    for side, reference in (
        ("accepted", pair.accepted_events),
        ("candidate", pair.candidate_events),
    ):
        source = root / reference.path
        rewritten = []
        for line in source.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            if evaluation_nonce is not None:
                payload["evaluation_nonce"] = evaluation_nonce
            if cost_currency is not None:
                payload["cost_currency"] = cost_currency
            if side == "accepted" and accepted_run_id is not None:
                payload["run_id"] = accepted_run_id
            if side == "candidate" and candidate_run_id is not None:
                payload["run_id"] = candidate_run_id
            rewritten.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        target = source.with_name(f"forged-{side}-events.jsonl")
        target.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        log_refs[side] = _workspace_ref(root, target)

    pair_updates: dict[str, object] = {
        "accepted_events": log_refs["accepted"],
        "candidate_events": log_refs["candidate"],
        "pair_execution_sha256": "0" * 64,
    }
    if measured_at is not None:
        pair_updates["measured_at"] = measured_at
    if evaluation_nonce is not None:
        pair_updates["evaluation_nonce"] = evaluation_nonce
    if cost_currency is not None:
        pair_updates["cost_currency"] = cost_currency
    if accepted_run_id is not None:
        pair_updates["accepted_run_id"] = accepted_run_id
    if candidate_run_id is not None:
        pair_updates["candidate_run_id"] = candidate_run_id
    forged_pair = pair.model_copy(update=pair_updates)
    forged_pair_path = pair_path.with_name("forged-selection-pair.json")
    forged_pair_path.write_bytes(artifact_json_bytes(forged_pair))
    pair_ref = _workspace_ref(root, forged_pair_path)
    steps = tuple(
        step.model_copy(
            update={
                "evidence": (
                    pair_ref,
                    log_refs["accepted"],
                    log_refs["candidate"],
                )
            }
        )
        if step.stage is GateStage.SELECTION
        else step.model_copy(update={"evidence": (pair_ref,)})
        if step.stage
        in {
            GateStage.CRITICAL_REGRESSION,
            GateStage.OVERALL_QUALITY,
            GateStage.COST,
            GateStage.BUDGET,
        }
        else step
        for step in decision.steps
    )
    updates: dict[str, object] = {"steps": steps}
    if cost_currency is not None:
        updates["metrics"] = decision.metrics.model_copy(
            update={"cost_currency": cost_currency}
        )
    return decision.model_copy(update=updates)


def _rewrite_last_event(
    registry: SkillRegistry,
    *,
    updates: dict[str, object],
) -> None:
    lines = registry.events_path.read_text(encoding="utf-8").splitlines()
    event = RegistryEvent.model_validate_json(lines[-1])
    forged = event.model_copy(update={**updates, "event_sha256": "0" * 64})
    lines[-1] = artifact_json_bytes(forged).decode("utf-8")
    registry.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _initialized(tmp_path: Path) -> tuple[SkillRegistry, Path, str]:
    root = tmp_path / "governance"
    registry = SkillRegistry(root)
    event = registry.initialize(
        command_id="command-initialize",
        accepted_skill=PARENT,
        evidence_paths=(SEED_EVIDENCE,),
        occurred_at=NOW,
    )
    return registry, root, event.version_sha256


def test_registry_records_accept_promote_and_rollback_without_mutating_history(
    tmp_path: Path,
) -> None:
    registry, root, initial_hash = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registered = registry.register_candidate(
        command_id="command-register",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    decision = _decision(root, candidate, FixedGateScenario.ACCEPT, "gate-registry")
    assert decision.outcome is GateOutcome.ACCEPTED
    registry.record_decision(
        command_id="command-accept",
        decision_path=root / "gates/gate-registry/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )
    registry.promote(
        command_id="command-promote",
        candidate_id=registered.version_id,
        occurred_at=NOW + timedelta(seconds=3),
    )
    promoted_state = registry.audit()
    assert promoted_state.current_accepted_sha256 == registered.version_sha256
    assert promoted_state.versions[registered.version_sha256].verified is True

    before = (root / "events.jsonl").read_bytes()
    registry.rollback(
        command_id="command-rollback",
        target_skill_sha256=initial_hash,
        occurred_at=NOW + timedelta(seconds=4),
    )
    state = registry.audit()

    assert state.current_accepted_sha256 == initial_hash
    assert state.versions[registered.version_sha256].status is VersionStatus.ROLLED_BACK
    assert (root / "events.jsonl").read_bytes().startswith(before)
    assert len(state.events) == 5
    assert normalized_skill_sha256(registry.version_path(initial_hash)) == initial_hash


def test_registry_replays_two_promoted_generations_with_stable_lineage(
    tmp_path: Path,
) -> None:
    registry, root, initial_hash = _initialized(tmp_path)
    first_candidate = _candidate(tmp_path)
    first = registry.register_candidate(
        command_id="command-register-generation-one",
        candidate_bundle=first_candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    _decision(
        root,
        first_candidate,
        FixedGateScenario.ACCEPT,
        "gate-generation-one",
    )
    registry.record_decision(
        command_id="command-accept-generation-one",
        decision_path=root / "gates/gate-generation-one/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )
    registry.promote(
        command_id="command-promote-generation-one",
        candidate_id=first.version_id,
        occurred_at=NOW + timedelta(seconds=3),
    )

    second_candidate = _second_generation_candidate(
        tmp_path,
        parent=registry.version_path(first.version_sha256),
        evidence_candidate=first_candidate,
    )
    second = registry.register_candidate(
        command_id="command-register-generation-two",
        candidate_bundle=second_candidate,
        occurred_at=NOW + timedelta(seconds=4),
    )
    run_candidate_gate(
        GateRequest(
            gate_id="gate-generation-two",
            lineage_id=registry.audit().lineage_id,
            workspace_root=root,
            accepted_skill=registry.version_path(first.version_sha256),
            candidate_bundle=second_candidate,
            selection_lock=SELECTION_LOCK,
            policy=default_gate_policy(ROOT, SELECTION_LOCK),
            mode="fixed",
            measured_at=NOW + timedelta(seconds=5),
        ),
        adapter=FixedGateAdapter(),
    )
    registry.record_decision(
        command_id="command-accept-generation-two",
        decision_path=root / "gates/gate-generation-two/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=6),
    )
    registry.promote(
        command_id="command-promote-generation-two",
        candidate_id=second.version_id,
        occurred_at=NOW + timedelta(seconds=7),
    )

    state = registry.audit()
    assert state.current_accepted_sha256 == second.version_sha256
    assert state.versions[first.version_sha256].parent_skill_sha256 == initial_hash
    assert (
        state.versions[second.version_sha256].parent_skill_sha256
        == first.version_sha256
    )
    assert state.versions[first.version_sha256].was_current is True
    assert state.versions[second.version_sha256].was_current is True
    assert len(state.events) == 7


def test_registry_retains_rejected_candidate_and_forbids_promotion(
    tmp_path: Path,
) -> None:
    registry, root, initial_hash = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registered = registry.register_candidate(
        command_id="command-register-rejected",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    decision = _decision(root, candidate, FixedGateScenario.TIE, "gate-rejected")
    assert decision.outcome is GateOutcome.REJECTED
    registry.record_decision(
        command_id="command-reject",
        decision_path=root / "gates/gate-rejected/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )

    with pytest.raises(RegistryError, match="accepted candidate"):
        registry.promote(
            command_id="command-invalid-promote",
            candidate_id=registered.version_id,
            occurred_at=NOW + timedelta(seconds=3),
        )
    with pytest.raises(RegistryError, match="verified historical version"):
        registry.rollback(
            command_id="command-invalid-rejected-rollback",
            target_skill_sha256=registered.version_sha256,
            occurred_at=NOW + timedelta(seconds=4),
        )

    state = registry.audit()
    assert state.current_accepted_sha256 == initial_hash
    assert state.versions[registered.version_sha256].status is VersionStatus.REJECTED
    assert registry.version_path(registered.version_sha256).is_dir()


def test_registry_records_candidate_validation_rejection_after_source_tampering(
    tmp_path: Path,
) -> None:
    registry, root, initial_hash = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registered = registry.register_candidate(
        command_id="command-register-before-source-tamper",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    skill_path = candidate / "skill/SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\nTampered after registration.\n",
        encoding="utf-8",
    )

    decision = _decision(
        root,
        candidate,
        FixedGateScenario.ACCEPT,
        "gate-candidate-source-tamper",
    )
    event = registry.record_decision(
        command_id="command-reject-source-tamper",
        decision_path=(root / "gates/gate-candidate-source-tamper/gate-decision.json"),
        occurred_at=NOW + timedelta(seconds=2),
    )

    state = registry.audit()
    assert decision.outcome is GateOutcome.REJECTED
    assert event.event_type.value == "candidate_rejected"
    assert state.current_accepted_sha256 == initial_hash
    assert state.versions[registered.version_sha256].status is VersionStatus.REJECTED
    assert (
        normalized_skill_sha256(registry.version_path(registered.version_sha256))
        == registered.version_sha256
    )


def test_registry_records_an_evidence_bound_trigger_rejection(tmp_path: Path) -> None:
    registry, root, initial_hash = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registered = registry.register_candidate(
        command_id="command-register-trigger-rejection",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    decision = _decision(
        root,
        candidate,
        FixedGateScenario.TRIGGER_FAILURE,
        "gate-trigger-rejection",
    )

    registry.record_decision(
        command_id="command-record-trigger-rejection",
        decision_path=root / "gates/gate-trigger-rejection/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )

    state = registry.audit()
    assert decision.outcome is GateOutcome.REJECTED
    assert state.current_accepted_sha256 == initial_hash
    assert state.versions[registered.version_sha256].status is VersionStatus.REJECTED


def test_registry_rejects_rehashed_rejection_metrics_without_appending(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-rejected-metrics",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    decision = _decision(
        root,
        candidate,
        FixedGateScenario.TIE,
        "gate-rejected-metrics",
    )
    forged = decision.model_copy(
        update={"metrics": decision.metrics.model_copy(update={"quality_delta": 0.5})}
    )
    forged_path = root / "gates/gate-rejected-metrics/forged-decision.json"
    forged_path.write_bytes(artifact_json_bytes(forged))
    before = registry.events_path.read_bytes()

    with pytest.raises(RegistryError, match="measured evidence"):
        registry.record_decision(
            command_id="command-record-rejected-metrics",
            decision_path=forged_path,
            occurred_at=NOW + timedelta(seconds=2),
        )

    assert registry.events_path.read_bytes() == before


def test_registry_commands_are_idempotent_but_command_ids_cannot_change_meaning(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    first = registry.register_candidate(
        command_id="command-idempotent",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    before = (root / "events.jsonl").read_bytes()
    second = registry.register_candidate(
        command_id="command-idempotent",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(days=1),
    )

    assert second == first
    assert (root / "events.jsonl").read_bytes() == before
    with pytest.raises(RegistryError, match="command_id conflict"):
        registry.rollback(
            command_id="command-idempotent",
            target_skill_sha256=first.version_sha256,
            occurred_at=NOW + timedelta(days=2),
        )


def test_registry_detects_event_decision_and_version_tampering(tmp_path: Path) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registered = registry.register_candidate(
        command_id="command-register",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    _decision(root, candidate, FixedGateScenario.ACCEPT, "gate-tamper")
    registry.record_decision(
        command_id="command-accept",
        decision_path=root / "gates/gate-tamper/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )

    event_lines = (root / "events.jsonl").read_text().splitlines()
    changed = json.loads(event_lines[1])
    changed["reason"] = "tampered"
    event_lines[1] = json.dumps(changed, sort_keys=True, separators=(",", ":"))
    (root / "events.jsonl").write_text("\n".join(event_lines) + "\n")
    with pytest.raises(RegistryError, match="hash"):
        registry.audit()

    # Restore from a fresh registry, then mutate immutable version content.
    registry2, root2, _ = _initialized(tmp_path / "second")
    candidate2 = _candidate(tmp_path / "second")
    registered2 = registry2.register_candidate(
        command_id="command-register",
        candidate_bundle=candidate2,
        occurred_at=NOW + timedelta(seconds=1),
    )
    (registry2.version_path(registered2.version_sha256) / "SKILL.md").write_text(
        "tampered", encoding="utf-8"
    )
    with pytest.raises(RegistryError, match="version"):
        registry2.audit()

    assert registered.version_sha256 == registered2.version_sha256
    assert root != root2


def test_rollback_requires_an_existing_verified_former_current_version(
    tmp_path: Path,
) -> None:
    registry, _, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registered = registry.register_candidate(
        command_id="command-register",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )

    for target in ("f" * 64, registered.version_sha256):
        with pytest.raises(RegistryError, match="verified historical version"):
            registry.rollback(
                command_id=f"command-rollback-{target[:4]}",
                target_skill_sha256=target,
                occurred_at=NOW + timedelta(seconds=2),
            )


def test_registry_rejects_a_gate_decision_bound_to_a_different_policy(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-policy",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    decision = _decision(root, candidate, FixedGateScenario.ACCEPT, "gate-policy")
    policy_path = root / decision.gate_policy.path
    policy = GatePolicy.model_validate_json(policy_path.read_text(encoding="utf-8"))
    forged_policy = policy.model_copy(update={"model_lock_sha256": "a" * 64})
    forged_path = policy_path.with_name("forged-policy.json")
    forged_bytes = artifact_json_bytes(forged_policy)
    forged_path.write_bytes(forged_bytes)
    forged_sha = hashlib.sha256(forged_bytes).hexdigest()
    forged_ref = ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=forged_path.relative_to(root).as_posix(),
        sha256=forged_sha,
    )
    forged_decision = decision.model_copy(
        update={
            "gate_policy": forged_ref,
            "gate_policy_sha256": forged_sha,
        }
    )
    forged_decision_path = forged_path.with_name("forged-decision.json")
    forged_decision_path.write_bytes(artifact_json_bytes(forged_decision))

    with pytest.raises(RegistryError, match="policy"):
        registry.record_decision(
            command_id="command-forged-policy",
            decision_path=forged_decision_path,
            occurred_at=NOW + timedelta(seconds=2),
        )


def test_registry_rejects_an_accepted_decision_without_static_evidence(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-static-forgery",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    decision = _decision(
        root,
        candidate,
        FixedGateScenario.ACCEPT,
        "gate-static-forgery",
    )
    forged_path = root / "gates/gate-static-forgery/forged-decision.json"
    payload = decision.model_dump(mode="json")
    for step in payload["steps"]:
        if step["stage"] == "static":
            step["evidence"] = []
    forged_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    before = registry.events_path.read_bytes()

    with pytest.raises(RegistryError, match="gate decision is invalid"):
        registry.record_decision(
            command_id="command-accept-static-forgery",
            decision_path=forged_path,
            occurred_at=NOW + timedelta(seconds=2),
        )

    assert registry.events_path.read_bytes() == before


@pytest.mark.parametrize(
    "forgery",
    ["one-row", "wrong-prompt", "wrong-model", "wrong-hash", "high-cost"],
)
def test_registry_rejects_forged_trigger_evidence_without_appending(
    tmp_path: Path,
    forgery: str,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-trigger-forgery",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    decision = _decision(
        root,
        candidate,
        FixedGateScenario.ACCEPT,
        "gate-trigger-forgery",
    )
    forged = _forged_trigger_decision(root, decision, forgery=forgery)
    forged_path = root / f"gates/gate-trigger-forgery/forged-{forgery}.json"
    forged_path.write_bytes(artifact_json_bytes(forged))
    before = registry.events_path.read_bytes()

    with pytest.raises(RegistryError, match=r"trigger|metrics|policy"):
        registry.record_decision(
            command_id=f"command-accept-trigger-{forgery}",
            decision_path=forged_path,
            occurred_at=NOW + timedelta(seconds=2),
        )

    assert registry.events_path.read_bytes() == before


@pytest.mark.parametrize("forgery", ["measured-at", "nonce", "currency"])
def test_registry_rejects_selection_pair_semantic_forgery_without_appending(
    tmp_path: Path,
    forgery: str,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-pair-forgery",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    decision = _decision(
        root,
        candidate,
        FixedGateScenario.ACCEPT,
        "gate-pair-forgery",
    )
    if forgery == "measured-at":
        forged = _forged_pair_decision(
            root,
            decision,
            measured_at=NOW - timedelta(days=1),
        )
    elif forgery == "nonce":
        forged = _forged_pair_decision(
            root,
            decision,
            evaluation_nonce="forged-selection-nonce",
        )
    else:
        forged = _forged_pair_decision(root, decision, cost_currency="EUR")
    forged_path = root / "gates/gate-pair-forgery/forged-decision.json"
    forged_path.write_bytes(artifact_json_bytes(forged))
    before = registry.events_path.read_bytes()

    with pytest.raises(RegistryError, match="selection pair"):
        registry.record_decision(
            command_id=f"command-accept-pair-{forgery}",
            decision_path=forged_path,
            occurred_at=NOW + timedelta(seconds=2),
        )

    assert registry.events_path.read_bytes() == before


def test_registry_rejects_a_nonzero_selection_iteration_without_appending(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-iteration-forgery",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    decision = _decision(
        root,
        candidate,
        FixedGateScenario.ACCEPT,
        "gate-iteration-forgery",
    )
    selection = next(
        step for step in decision.steps if step.stage is GateStage.SELECTION
    )
    pair_path = root / selection.evidence[0].path
    pair_payload = json.loads(pair_path.read_text(encoding="utf-8"))
    pair_payload["iteration_id"] = "iteration-1"
    forged_pair_path = pair_path.with_name("forged-iteration-pair.json")
    forged_pair_path.write_text(
        json.dumps(pair_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    pair_ref = _workspace_ref(root, forged_pair_path)
    forged_steps = tuple(
        step.model_copy(
            update={
                "evidence": (
                    pair_ref,
                    selection.evidence[1],
                    selection.evidence[2],
                )
            }
        )
        if step.stage is GateStage.SELECTION
        else step.model_copy(update={"evidence": (pair_ref,)})
        if step.stage
        in {
            GateStage.CRITICAL_REGRESSION,
            GateStage.OVERALL_QUALITY,
            GateStage.COST,
            GateStage.BUDGET,
        }
        else step
        for step in decision.steps
    )
    forged = decision.model_copy(update={"steps": forged_steps})
    forged_path = root / "gates/gate-iteration-forgery/forged-decision.json"
    forged_path.write_bytes(artifact_json_bytes(forged))
    before = registry.events_path.read_bytes()

    with pytest.raises(RegistryError, match="selection pair evidence is invalid"):
        registry.record_decision(
            command_id="command-record-iteration-forgery",
            decision_path=forged_path,
            occurred_at=NOW + timedelta(seconds=2),
        )

    assert registry.events_path.read_bytes() == before


def test_registry_rejects_downstream_stage_evidence_rebound_from_the_pair(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-downstream-evidence",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    decision = _decision(
        root,
        candidate,
        FixedGateScenario.ACCEPT,
        "gate-downstream-evidence",
    )
    trigger = next(step for step in decision.steps if step.stage is GateStage.TRIGGER)
    forged_steps = tuple(
        step.model_copy(update={"evidence": trigger.evidence})
        if step.stage is GateStage.CRITICAL_REGRESSION
        else step
        for step in decision.steps
    )
    forged = decision.model_copy(update={"steps": forged_steps})
    forged_path = root / "gates/gate-downstream-evidence/forged-decision.json"
    forged_path.write_bytes(artifact_json_bytes(forged))
    before = registry.events_path.read_bytes()

    with pytest.raises(RegistryError, match="bind the selection pair"):
        registry.record_decision(
            command_id="command-accept-downstream-evidence",
            decision_path=forged_path,
            occurred_at=NOW + timedelta(seconds=2),
        )

    assert registry.events_path.read_bytes() == before


def test_registry_rejects_reused_selection_run_ids_without_appending(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    first_candidate = _candidate(tmp_path)
    second_candidate = _variant_candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-fresh-first",
        candidate_bundle=first_candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    registry.register_candidate(
        command_id="command-register-fresh-second",
        candidate_bundle=second_candidate,
        occurred_at=NOW + timedelta(seconds=2),
    )
    first = _decision(
        root,
        first_candidate,
        FixedGateScenario.ACCEPT,
        "gate-fresh-first",
    )
    registry.record_decision(
        command_id="command-accept-fresh-first",
        decision_path=root / "gates/gate-fresh-first/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=3),
    )
    first_selection = next(
        step for step in first.steps if step.stage is GateStage.SELECTION
    )
    first_pair = SelectionPairEvaluation.model_validate_json(
        (root / first_selection.evidence[0].path).read_text(encoding="utf-8")
    )
    second = _decision(
        root,
        second_candidate,
        FixedGateScenario.ACCEPT,
        "gate-fresh-second",
    )
    forged = _forged_pair_decision(
        root,
        second,
        accepted_run_id=first_pair.accepted_run_id,
        candidate_run_id=first_pair.candidate_run_id,
    )
    forged_path = root / "gates/gate-fresh-second/forged-decision.json"
    forged_path.write_bytes(artifact_json_bytes(forged))
    before = registry.events_path.read_bytes()

    with pytest.raises(RegistryError, match="fresh within the lineage"):
        registry.record_decision(
            command_id="command-accept-reused-runs",
            decision_path=forged_path,
            occurred_at=NOW + timedelta(seconds=4),
        )

    assert registry.events_path.read_bytes() == before


def test_registry_accepts_gate_time_normalized_from_a_non_utc_offset(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-offset-time",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    local_time = datetime(
        2026,
        8,
        18,
        17,
        tzinfo=timezone(timedelta(hours=8)),
    )
    decision = run_candidate_gate(
        GateRequest(
            gate_id="gate-offset-time",
            lineage_id=registry.audit().lineage_id,
            workspace_root=root,
            accepted_skill=PARENT,
            candidate_bundle=candidate,
            selection_lock=SELECTION_LOCK,
            policy=default_gate_policy(ROOT, SELECTION_LOCK),
            mode="fixed",
            measured_at=local_time,
        ),
        adapter=FixedGateAdapter(FixedGateScenario.ACCEPT),
    )

    event = registry.record_decision(
        command_id="command-accept-offset-time",
        decision_path=root / "gates/gate-offset-time/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )

    assert decision.decided_at == NOW
    assert registry.audit().versions[event.version_sha256].verified is True


def test_registry_initialization_requires_evidence_bound_to_the_skill(
    tmp_path: Path,
) -> None:
    registry = SkillRegistry(tmp_path / "registry")
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text('{"record_type":"unrelated"}', encoding="utf-8")

    with pytest.raises(RegistryError, match="verified accepted Skill"):
        registry.initialize(
            command_id="command-unverified-initialize",
            accepted_skill=PARENT,
            evidence_paths=(unrelated,),
            occurred_at=NOW,
        )

    assert not registry.events_path.exists()


def test_registry_rejects_a_stale_accepted_candidate_without_appending(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    first_candidate = _candidate(tmp_path)
    second_candidate = _variant_candidate(tmp_path)
    first = registry.register_candidate(
        command_id="command-register-first",
        candidate_bundle=first_candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    second = registry.register_candidate(
        command_id="command-register-second",
        candidate_bundle=second_candidate,
        occurred_at=NOW + timedelta(seconds=2),
    )
    _decision(root, first_candidate, FixedGateScenario.ACCEPT, "gate-first")
    registry.record_decision(
        command_id="command-accept-first",
        decision_path=root / "gates/gate-first/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=3),
    )
    _decision(root, second_candidate, FixedGateScenario.ACCEPT, "gate-second")
    registry.record_decision(
        command_id="command-accept-second",
        decision_path=root / "gates/gate-second/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=4),
    )
    registry.promote(
        command_id="command-promote-first",
        candidate_id=first.version_id,
        occurred_at=NOW + timedelta(seconds=5),
    )
    before = registry.events_path.read_bytes()

    with pytest.raises(RegistryError, match="stale"):
        registry.promote(
            command_id="command-promote-second",
            candidate_id=second.version_id,
            occurred_at=NOW + timedelta(seconds=6),
        )

    assert registry.events_path.read_bytes() == before
    assert registry.audit().current_accepted_sha256 == first.version_sha256


def test_registry_detects_gate_decision_and_seed_evidence_tampering(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-decision-tamper",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    _decision(root, candidate, FixedGateScenario.ACCEPT, "gate-decision-tamper")
    registry.record_decision(
        command_id="command-record-decision-tamper",
        decision_path=root / "gates/gate-decision-tamper/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )
    decision_path = root / "gates/gate-decision-tamper/gate-decision.json"
    decision_path.write_bytes(decision_path.read_bytes() + b" ")

    with pytest.raises(RegistryError, match="hash mismatch"):
        registry.audit()

    registry2, root2, _ = _initialized(tmp_path / "seed")
    evidence_path = next((root2 / "objects/evidence").iterdir())
    evidence_path.write_bytes(evidence_path.read_bytes() + b" ")
    with pytest.raises(RegistryError, match="hash mismatch"):
        registry2.audit()


@pytest.mark.parametrize(
    ("scenario", "gate_id"),
    [
        (FixedGateScenario.ACCEPT, "gate-rehash-accepted"),
        (FixedGateScenario.TIE, "gate-rehash-rejected"),
    ],
)
def test_registry_rejects_rehashed_candidate_event_with_stripped_evidence(
    tmp_path: Path,
    scenario: FixedGateScenario,
    gate_id: str,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-rehash-decision",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    _decision(root, candidate, scenario, gate_id)
    registry.record_decision(
        command_id="command-record-rehash-decision",
        decision_path=root / f"gates/{gate_id}/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )
    _rewrite_last_event(registry, updates={"evidence": ()})

    with pytest.raises(RegistryError, match="event evidence"):
        registry.audit()


def test_registry_rejects_rehashed_promotion_event_with_stripped_evidence(
    tmp_path: Path,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registered = registry.register_candidate(
        command_id="command-register-rehash-promote",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    _decision(root, candidate, FixedGateScenario.ACCEPT, "gate-rehash-promote")
    registry.record_decision(
        command_id="command-accept-rehash-promote",
        decision_path=root / "gates/gate-rehash-promote/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )
    registry.promote(
        command_id="command-rehash-promote",
        candidate_id=registered.version_id,
        occurred_at=NOW + timedelta(seconds=3),
    )
    _rewrite_last_event(registry, updates={"evidence": ()})

    with pytest.raises(RegistryError, match="promotion transition"):
        registry.audit()


def test_registry_rejects_rehashed_rollback_event_with_wrong_evidence(
    tmp_path: Path,
) -> None:
    registry, root, initial_hash = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registered = registry.register_candidate(
        command_id="command-register-rehash-rollback",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    _decision(root, candidate, FixedGateScenario.ACCEPT, "gate-rehash-rollback")
    registry.record_decision(
        command_id="command-accept-rehash-rollback",
        decision_path=root / "gates/gate-rehash-rollback/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )
    registry.promote(
        command_id="command-promote-rehash-rollback",
        candidate_id=registered.version_id,
        occurred_at=NOW + timedelta(seconds=3),
    )
    registry.rollback(
        command_id="command-rehash-rollback",
        target_skill_sha256=initial_hash,
        occurred_at=NOW + timedelta(seconds=4),
    )
    event = RegistryEvent.model_validate_json(
        registry.events_path.read_text(encoding="utf-8").splitlines()[-1]
    )
    _rewrite_last_event(registry, updates={"evidence": (event.version_manifest,)})

    with pytest.raises(RegistryError, match="rollback transition"):
        registry.audit()


@pytest.mark.parametrize(
    "tamper",
    ["reorder", "delete-middle", "forge-tail", "zero-tail-hash"],
)
def test_registry_detects_event_chain_structure_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    registry, root, _ = _initialized(tmp_path)
    candidate = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-chain",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    _decision(root, candidate, FixedGateScenario.TIE, "gate-chain")
    registry.record_decision(
        command_id="command-record-chain",
        decision_path=root / "gates/gate-chain/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=2),
    )
    lines = registry.events_path.read_text(encoding="utf-8").splitlines()
    if tamper == "reorder":
        lines[1], lines[2] = lines[2], lines[1]
    elif tamper == "delete-middle":
        del lines[1]
    elif tamper == "forge-tail":
        lines.append(lines[-1])
    else:
        tail = json.loads(lines[-1])
        tail["event_sha256"] = "0" * 64
        lines[-1] = json.dumps(tail, sort_keys=True, separators=(",", ":"))
    registry.events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(RegistryError):
        registry.audit()


def test_registry_snapshot_is_unchanged_when_the_external_parent_is_modified(
    tmp_path: Path,
) -> None:
    external_parent = tmp_path / "external-parent"
    shutil.copytree(PARENT, external_parent)
    registry = SkillRegistry(tmp_path / "registry")
    initialized = registry.initialize(
        command_id="command-external-parent",
        accepted_skill=external_parent,
        evidence_paths=(SEED_EVIDENCE,),
        occurred_at=NOW,
    )
    stored_before = {
        path.relative_to(
            registry.version_path(initialized.version_sha256)
        ): path.read_bytes()
        for path in registry.version_path(initialized.version_sha256).rglob("*")
        if path.is_file()
    }
    (external_parent / "SKILL.md").write_text("tampered", encoding="utf-8")

    state = registry.audit()

    assert state.current_accepted_sha256 == initialized.version_sha256
    assert stored_before == {
        path.relative_to(
            registry.version_path(initialized.version_sha256)
        ): path.read_bytes()
        for path in registry.version_path(initialized.version_sha256).rglob("*")
        if path.is_file()
    }
