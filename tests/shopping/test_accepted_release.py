from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from ses.automation.orchestrator import final_execution_run_set_sha256
from ses.cli.app import main
from ses.contracts import (
    AcceptedSkillReleaseManifest,
    ArtifactRef,
    ArtifactRoot,
    AutoEvolveState,
    AutoLoopStatus,
    AutoStopReason,
    CapstoneFinalReceipt,
    FinalAggregateReport,
    FinalConsumedCheckpoint,
    FinalRunReceipt,
    MeasurementKind,
    Patch,
    SchemaVersion,
    ShoppingFinalScenarioMetrics,
    artifact_json_bytes,
)
from ses.contracts.runner import RunRecord
from ses.contracts.shopping import (
    PurchaseAttemptReceipt,
    ShoppingActionKind,
    ShoppingActionReceipt,
    ShoppingActionRequest,
    ShoppingMetricProjection,
    ShoppingScenario,
    ShopSimulatorEpisodeResult,
)
from ses.evolution.gate import (
    FixedGateAdapter,
    FixedGateScenario,
    GateRequest,
    default_gate_policy,
    run_candidate_gate,
)
from ses.evolution.registry import SkillRegistry
from ses.evolution.updater import FakeUpdater, UpdaterRequest
from ses.evolution.workflow import run_evolution_workflow
from ses.shopping.automation import build_shopping_capstone_orchestrator
from ses.shopping.course_workflow import (
    run_shopping_create_stage,
    run_shopping_paired_stage,
    run_shopping_static_stage,
    run_shopping_trigger_stage,
)
from ses.shopping.fixed_course import build_fixed_develop_evaluation
from ses.shopping.grading import build_shopping_case_grade
from ses.shopping.manual_workflow import (
    promote_shopping_candidate,
    register_shopping_candidate,
    run_shopping_evolution_stage,
    run_shopping_gate_stage,
)
from ses.shopping.profile import load_shopping_profile
from ses.shopping.registry import open_shopping_registry
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256
from ses.skills.release import (
    AcceptedSkillReleaseError,
    install_current_accepted,
    package_current_accepted,
)
from ses.skills.workflow import SkillV0WorkflowConfig, run_skill_v0_workflow

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FAILURE_EVIDENCE = (
    PROJECT_ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
)
SELECTION_LOCK = PROJECT_ROOT / "data/testset/protected/selection-manifest.json"
SEED_MANIFEST = PROJECT_ROOT / "data/skill-v0/creator/seed-manifest.json"
CAPSTONE = PROJECT_ROOT / "fixtures/seed/capstone-shopping-assistant"
NOW = datetime(2026, 8, 19, 9, tzinfo=UTC)


class _VariantUpdater(FakeUpdater):
    def propose(self, request: UpdaterRequest) -> Patch:
        original = super().propose(request)
        operations = tuple(
            operation.model_copy(
                update={
                    "content": operation.content
                    + "\nPrefer the narrowest eligible option when unsure.\n"
                }
            )
            if operation.operation == "update"
            else operation
            for operation in original.operations
        )
        return Patch(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="skill_patch",
            patch_id="patch-release-rejected-variant",
            parent_skill_sha256=original.parent_skill_sha256,
            operations=operations,
        )


def _ref(root: Path, path: Path) -> ArtifactRef:
    payload = path.read_bytes()
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _run_ref(run_root: Path, path: Path) -> ArtifactRef:
    payload = path.read_bytes()
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.relative_to(run_root).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _write_canonical_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _accepted_registry(tmp_path: Path) -> SkillRegistry:
    v0_root = tmp_path / "v0"
    run_skill_v0_workflow(
        SkillV0WorkflowConfig(
            project_root=PROJECT_ROOT,
            output_root=v0_root,
            seed_manifest=SEED_MANIFEST,
            mode="fixed",
        )
    )
    parent = v0_root / "skill/v0"
    registry = SkillRegistry(tmp_path / "registry")
    registry.initialize(
        command_id="command-release-initialize",
        accepted_skill=parent,
        evidence_paths=(v0_root / "summary.json",),
        occurred_at=NOW,
    )
    candidate = tmp_path / "candidate"
    run_evolution_workflow(
        parent_dir=parent,
        evidence_path=FAILURE_EVIDENCE,
        output_root=candidate,
        updater=FakeUpdater(),
        mode="fixed",
    )
    registered = registry.register_candidate(
        command_id="command-release-register",
        candidate_bundle=candidate,
        occurred_at=NOW + timedelta(seconds=1),
    )
    rejected_candidate = tmp_path / "rejected-candidate"
    run_evolution_workflow(
        parent_dir=parent,
        evidence_path=FAILURE_EVIDENCE,
        output_root=rejected_candidate,
        updater=_VariantUpdater(),
        mode="fixed",
    )
    registry.register_candidate(
        command_id="command-release-register-rejected",
        candidate_bundle=rejected_candidate,
        occurred_at=NOW + timedelta(seconds=2),
    )
    gate_id = "gate-release-accepted"
    run_candidate_gate(
        GateRequest(
            gate_id=gate_id,
            lineage_id=registry.audit().lineage_id,
            workspace_root=registry.root,
            accepted_skill=parent,
            candidate_bundle=candidate,
            selection_lock=SELECTION_LOCK,
            policy=default_gate_policy(PROJECT_ROOT, SELECTION_LOCK),
            mode="fixed",
            measured_at=NOW + timedelta(seconds=3),
        ),
        adapter=FixedGateAdapter(),
    )
    registry.record_decision(
        command_id="command-release-accept",
        decision_path=registry.root / f"gates/{gate_id}/gate-decision.json",
        occurred_at=NOW + timedelta(seconds=4),
    )
    rejected_gate_id = "gate-release-rejected"
    run_candidate_gate(
        GateRequest(
            gate_id=rejected_gate_id,
            lineage_id=registry.audit().lineage_id,
            workspace_root=registry.root,
            accepted_skill=parent,
            candidate_bundle=rejected_candidate,
            selection_lock=SELECTION_LOCK,
            policy=default_gate_policy(PROJECT_ROOT, SELECTION_LOCK),
            mode="fixed",
            measured_at=NOW + timedelta(seconds=5),
        ),
        adapter=FixedGateAdapter(FixedGateScenario.TIE),
    )
    registry.record_decision(
        command_id="command-release-reject",
        decision_path=(registry.root / f"gates/{rejected_gate_id}/gate-decision.json"),
        occurred_at=NOW + timedelta(seconds=6),
    )
    registry.promote(
        command_id="command-release-promote",
        candidate_id=registered.version_id,
        occurred_at=NOW + timedelta(seconds=7),
    )
    return registry


def _capstone_final_receipt(
    workspace_root: Path,
    *,
    registry: SkillRegistry,
    safety_violation_count: int = 0,
    directory: str = "final",
    result_source: Literal[
        "fixed_reference", "fresh_fixed_execution"
    ] = "fresh_fixed_execution",
) -> Path:
    final_root = workspace_root / directory
    final_root.mkdir()
    state = registry.audit()
    subject = state.current_accepted_sha256
    experiment_id = "experiment-shopping-release"
    final_lock = "1" * 64
    case_passes = (True,) * 12
    private_details = {
        "experiment_id": experiment_id,
        "episode_results": [],
        "events_sha256": "8" * 64,
        "final_manifest_sha256": final_lock,
        "measurement_kind": "synthetic_offline",
        "result_source": result_source,
        "run_id": "run-shopping-final-current-fixed",
        "subject_skill_sha256": subject,
    }
    private_path = final_root / "private-results.json"
    private_path.write_bytes(
        json.dumps(
            {
                "case_passes": case_passes,
                "details": private_details,
                "executed_at": (NOW + timedelta(seconds=5))
                .isoformat()
                .replace("+00:00", "Z"),
                "experiment_id": experiment_id,
                "safety_violation_count": safety_violation_count,
                "subject_skill_sha256": subject,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    private_sha = hashlib.sha256(private_path.read_bytes()).hexdigest()
    aggregate = FinalAggregateReport(
        schema_version=SchemaVersion.V1ALPHA2,
        record_type="final_aggregate_report",
        experiment_id=experiment_id,
        subject_skill_sha256=subject,
        final_lock_sha256=final_lock,
        mode="fixed",
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        network_used=False,
        result_source=result_source,
        executed_at=NOW + timedelta(seconds=5),
        case_count=12,
        pass_count=12,
        pass_rate=1.0,
        cost_amount=Decimal("0"),
        cost_currency="USD",
        cost_complete=True,
        input_tokens=0,
        output_tokens=0,
        private_results_sha256=private_sha,
        full_success_count=12,
        mean_strict_reward=Decimal("1"),
        safety_violation_count=safety_violation_count,
        scenario_metrics=tuple(
            ShoppingFinalScenarioMetrics(
                scenario=scenario,
                case_count=3,
                full_success_count=3,
                mean_strict_reward=Decimal("1"),
                safety_violation_count=(
                    safety_violation_count
                    if index == len(tuple(ShoppingScenario)) - 1
                    else 0
                ),
            )
            for index, scenario in enumerate(ShoppingScenario)
        ),
    )
    aggregate_path = final_root / "final-aggregate.json"
    aggregate_path.write_bytes(artifact_json_bytes(aggregate))
    run_receipt = FinalRunReceipt(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="final_run_receipt",
        experiment_id=experiment_id,
        subject_skill_sha256=subject,
        final_lock_sha256=final_lock,
        mode="fixed",
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        network_used=False,
        engine_id="fixed-final-engine-v1",
        simulator_id="fixed-shopping-simulator-v1",
        judge_id="fixed-shopping-judge-v1",
        provider_id="none-offline",
        model_lock_sha256="2" * 64,
        evaluation_protocol_sha256="3" * 64,
        report_protocol_sha256="4" * 64,
        executed_at=aggregate.executed_at,
        run_set_sha256=final_execution_run_set_sha256(
            case_passes=case_passes,
            private_payload=private_details,
        ),
        private_results_sha256=private_sha,
        aggregate_report_sha256=hashlib.sha256(aggregate_path.read_bytes()).hexdigest(),
        cost_amount=Decimal("0"),
        cost_currency="USD",
        cost_complete=True,
        input_tokens=0,
        output_tokens=0,
    )
    run_receipt_path = final_root / "final-run-receipt.json"
    run_receipt_path.write_bytes(artifact_json_bytes(run_receipt))
    checkpoint = FinalConsumedCheckpoint(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="final_consumed_checkpoint",
        experiment_id=experiment_id,
        subject_skill_sha256=subject,
        final_lock_sha256=final_lock,
        consumed=True,
        final_run_receipt_sha256=hashlib.sha256(
            run_receipt_path.read_bytes()
        ).hexdigest(),
        aggregate_report_sha256=run_receipt.aggregate_report_sha256,
        private_results_sha256=private_sha,
    )
    checkpoint_path = final_root / "final-consumed.checkpoint.json"
    checkpoint_path.write_bytes(artifact_json_bytes(checkpoint))
    capstone = CapstoneFinalReceipt(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="capstone_final_receipt",
        experiment_id=experiment_id,
        lineage_id=state.lineage_id,
        profile_sha256="6" * 64,
        subject_skill_sha256=subject,
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        completed=True,
        safety_violation_count=safety_violation_count,
        result_origin="fresh_fixed_execution",
        aggregate=_ref(workspace_root, aggregate_path),
        final_run_receipt=_ref(workspace_root, run_receipt_path),
        one_time_checkpoint=_ref(workspace_root, checkpoint_path),
    )
    capstone_path = final_root / "capstone-final-receipt.json"
    capstone_path.write_bytes(artifact_json_bytes(capstone))
    loop_state = AutoEvolveState(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="auto_evolve_state",
        experiment_id=experiment_id,
        config_sha256="7" * 64,
        status=(
            AutoLoopStatus.FINAL_COMPLETE
            if safety_violation_count == 0
            else AutoLoopStatus.FAILED_FINAL
        ),
        current_accepted_skill_sha256=subject,
        completed_rounds=0,
        rounds=(),
        total_cost_amount=Decimal("0"),
        cost_currency="USD",
        cost_complete=True,
        total_input_tokens=0,
        total_output_tokens=0,
        final_cost_amount=Decimal("0"),
        final_cost_complete=True,
        final_input_tokens=0,
        final_output_tokens=0,
        consecutive_rejections=0,
        stopped_at=NOW + timedelta(seconds=4),
        stop_reason=AutoStopReason.MAX_ROUNDS,
        final_report=_ref(workspace_root, aggregate_path),
    )
    (workspace_root / "state.json").write_bytes(artifact_json_bytes(loop_state))
    return capstone_path


@pytest.fixture(scope="module")
def release_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    workspace = tmp_path_factory.mktemp("accepted-release-template")
    profile = load_shopping_profile(CAPSTONE / "profiles/fixed-v1.json")
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE / "fixtures/creator-projections",
        experiment_root=workspace,
    )
    static = run_shopping_static_stage(
        profile=profile,
        experiment_root=workspace,
        skill_source=created.skill_source,
        create_receipt=created.receipt_path,
    )
    trigger = run_shopping_trigger_stage(
        profile=profile,
        experiment_root=workspace,
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
        experiment_root=workspace,
        skill_source=created.skill_source,
        trigger_receipt=trigger.receipt_path,
        tasks=fixed.tasks,
        baseline_evaluator=fixed.baseline_evaluator,
        skill_evaluator=fixed.skill_evaluator,
    )
    evolved = run_shopping_evolution_stage(
        profile=profile,
        experiment_root=workspace,
        paired_receipt=paired.receipt_path,
    )
    registry = open_shopping_registry(workspace / "registry")
    registry.initialize(
        command_id="command-shopping-initialize",
        accepted_skill=created.skill_source,
        evidence_paths=(workspace / "v0-pipeline-summary.json",),
        occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
        lineage_id=f"lineage-shopping-fixed-{profile.profile_sha256[:16]}",
    )
    register_shopping_candidate(
        registry_root=registry.root,
        candidate_bundle=evolved.candidate_bundle,
    )
    manual = run_shopping_gate_stage(
        profile=profile,
        experiment_root=workspace,
        registry_root=registry.root,
        candidate_bundle=evolved.candidate_bundle,
    )
    promote_shopping_candidate(
        registry_root=registry.root,
        decision_path=manual.decision_path,
        candidate_id=manual.decision.candidate_id,
    )
    orchestrator = build_shopping_capstone_orchestrator(
        profile=profile,
        project_root=PROJECT_ROOT,
        experiment_root=workspace,
    )
    orchestrator.run()
    orchestrator.run_final_once()
    package_current_accepted(
        workspace_root=workspace,
        registry=registry,
        capstone_final_receipt=workspace / "final/capstone-final-receipt.json",
        output=workspace / "package",
        released_at=NOW + timedelta(seconds=8),
    )
    return workspace


def _copy_release_template(
    template: Path, tmp_path: Path
) -> tuple[Path, SkillRegistry]:
    workspace = tmp_path / "workspace"
    shutil.copytree(template, workspace)
    return workspace, open_shopping_registry(workspace / "registry")


def _rebind_private_final(
    workspace: Path,
    private: dict[str, object],
    *,
    run_set_sha256: str | None = None,
) -> None:
    final_root = workspace / "final"
    private_path = final_root / "private-results.json"
    private_path.write_bytes(
        json.dumps(
            private,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    private_sha256 = hashlib.sha256(private_path.read_bytes()).hexdigest()

    aggregate_path = final_root / "final-aggregate.json"
    aggregate = FinalAggregateReport.model_validate_json(aggregate_path.read_bytes())
    aggregate = aggregate.model_copy(update={"private_results_sha256": private_sha256})
    aggregate_path.write_bytes(artifact_json_bytes(aggregate))
    aggregate_sha256 = hashlib.sha256(aggregate_path.read_bytes()).hexdigest()

    run_path = final_root / "final-run-receipt.json"
    run = FinalRunReceipt.model_validate_json(run_path.read_bytes())
    case_passes = private["case_passes"]
    details = private["details"]
    assert isinstance(case_passes, list)
    assert isinstance(details, dict)
    run = run.model_copy(
        update={
            "aggregate_report_sha256": aggregate_sha256,
            "private_results_sha256": private_sha256,
            "run_set_sha256": run_set_sha256
            or final_execution_run_set_sha256(
                case_passes=tuple(case_passes),
                private_payload=details,
            ),
        }
    )
    run_path.write_bytes(artifact_json_bytes(run))

    checkpoint_path = workspace / "final-consumed.checkpoint.json"
    checkpoint = FinalConsumedCheckpoint.model_validate_json(
        checkpoint_path.read_bytes()
    ).model_copy(
        update={
            "aggregate_report_sha256": aggregate_sha256,
            "final_run_receipt_sha256": hashlib.sha256(
                run_path.read_bytes()
            ).hexdigest(),
            "private_results_sha256": private_sha256,
        }
    )
    checkpoint_path.write_bytes(artifact_json_bytes(checkpoint))

    capstone_path = final_root / "capstone-final-receipt.json"
    capstone = CapstoneFinalReceipt.model_validate_json(capstone_path.read_bytes())
    capstone = capstone.model_copy(
        update={
            "aggregate": _ref(workspace, aggregate_path),
            "final_run_receipt": _ref(workspace, run_path),
            "one_time_checkpoint": _ref(workspace, checkpoint_path),
        }
    )
    capstone_path.write_bytes(artifact_json_bytes(capstone))

    state_path = workspace / "state.json"
    state = AutoEvolveState.model_validate_json(state_path.read_bytes()).model_copy(
        update={"final_report": _ref(workspace, aggregate_path)}
    )
    state_path.write_bytes(artifact_json_bytes(state))


def test_current_accepted_with_clean_final_can_be_packaged_and_installed(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    package_root = workspace / "package"
    release = AcceptedSkillReleaseManifest.model_validate_json(
        (package_root / "release-manifest.json").read_bytes()
    )

    state = registry.audit()
    source = registry.version_path(state.current_accepted_sha256)
    runtime_manifest = load_skill_manifest(source)
    packaged_skill = package_root / "skill"
    assert release.accepted_skill_sha256 == state.current_accepted_sha256
    assert release.package_sha256 == state.current_accepted_sha256
    assert release.runtime_files == runtime_manifest.files
    assert normalized_skill_sha256(packaged_skill) == state.current_accepted_sha256
    assert {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file()
    } == {
        "release-manifest.json",
        "skill/skill-manifest.json",
        *(f"skill/{item.path}" for item in runtime_manifest.files),
    }

    installation = install_current_accepted(
        workspace_root=workspace,
        release_manifest=package_root / "release-manifest.json",
        destination=workspace / "installed-skills",
        registry=registry,
    )

    assert installation.destination == (
        workspace / "installed-skills" / runtime_manifest.name
    )
    assert installation.sha256 == state.current_accepted_sha256
    assert installation.installed_files == tuple(
        item.path for item in runtime_manifest.files
    )


def test_release_rejects_private_final_with_one_case_claiming_twelve(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    shutil.rmtree(workspace / "package")
    private_path = workspace / "final/private-results.json"
    private = json.loads(private_path.read_text(encoding="utf-8"))
    private["case_passes"] = [True]
    _rebind_private_final(workspace, private)

    with pytest.raises(AcceptedSkillReleaseError, match="private final evidence"):
        package_current_accepted(
            workspace_root=workspace,
            registry=registry,
            capstone_final_receipt=(workspace / "final/capstone-final-receipt.json"),
            output=workspace / "forged-private-package",
            released_at=NOW + timedelta(seconds=9),
        )


def test_release_rejects_a_nonterminal_auto_evolve_state(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    shutil.rmtree(workspace / "package")
    state_path = workspace / "state.json"
    state = AutoEvolveState.model_validate_json(state_path.read_bytes())
    state = state.model_copy(
        update={
            "status": AutoLoopStatus.FAILED_FINAL,
        }
    )
    state_path.write_bytes(artifact_json_bytes(state))

    with pytest.raises(AcceptedSkillReleaseError, match="auto-evolve state"):
        package_current_accepted(
            workspace_root=workspace,
            registry=registry,
            capstone_final_receipt=(workspace / "final/capstone-final-receipt.json"),
            output=workspace / "nonterminal-state-package",
            released_at=NOW + timedelta(seconds=9),
        )


def test_release_recomputes_the_private_final_run_set(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    shutil.rmtree(workspace / "package")
    private = json.loads(
        (workspace / "final/private-results.json").read_text(encoding="utf-8")
    )
    _rebind_private_final(workspace, private, run_set_sha256="f" * 64)

    with pytest.raises(AcceptedSkillReleaseError, match="protocol receipts"):
        package_current_accepted(
            workspace_root=workspace,
            registry=registry,
            capstone_final_receipt=(workspace / "final/capstone-final-receipt.json"),
            output=workspace / "forged-run-set-package",
            released_at=NOW + timedelta(seconds=9),
        )


def test_release_rejects_unauthorized_purchase_hidden_by_resigned_safe_layers(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    shutil.rmtree(workspace / "package")
    private = json.loads(
        (workspace / "final/private-results.json").read_text(encoding="utf-8")
    )
    first = private["details"]["episode_results"][0]
    run_root = workspace / "final/protected-evaluation" / private["details"]["run_id"]
    episode_path = workspace / first["episode_result"]
    attempt_root = episode_path.parent
    episode = ShopSimulatorEpisodeResult.model_validate_json(episode_path.read_bytes())

    trace_path = run_root / episode.traces[0].path
    trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
    trace_payload["events"][0]["payload"].update(
        {
            "arguments": {"action_id": "forged-buy-action"},
            "tool_name": "mcp__shop_simulator__purchase",
        }
    )
    _write_canonical_json(trace_path, trace_payload)
    trace_ref = _run_ref(run_root, trace_path)

    action_path = run_root / episode.action_receipts[0].path
    action = ShoppingActionReceipt.model_validate_json(action_path.read_bytes())
    request = ShoppingActionRequest(
        kind=ShoppingActionKind.PURCHASE,
        action_id="forged-buy-action",
    )
    intent_path = attempt_root / action.intent.path
    _write_canonical_json(
        intent_path,
        {
            "episode_nonce": action.episode_nonce,
            "observation_sha256": action.observation_sha256,
            "record_type": "shopping_action_intent",
            "request": request.model_dump(mode="json"),
            "schema_version": "v1alpha1",
            "turn_lease_id": action.turn_lease_id,
            "turn_sequence": action.turn_sequence,
        },
    )
    intent_ref = ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=intent_path.relative_to(attempt_root).as_posix(),
        sha256=hashlib.sha256(intent_path.read_bytes()).hexdigest(),
    )
    action = action.model_copy(
        update={
            "action_kind": ShoppingActionKind.PURCHASE,
            "request": request,
            "intent": intent_ref,
        }
    )
    action_path.write_bytes(artifact_json_bytes(action))
    action_ref = _run_ref(run_root, action_path)

    authorization_path = attempt_root / "authorization-messages/forged.json"
    _write_canonical_json(
        authorization_path,
        {
            "record_type": "shopping_authorization_message",
            "schema_version": "v1alpha1",
            "state": "denied",
        },
    )
    authorization_ref = ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=authorization_path.relative_to(attempt_root).as_posix(),
        sha256=hashlib.sha256(authorization_path.read_bytes()).hexdigest(),
    )
    purchase_attempt = PurchaseAttemptReceipt(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="purchase_attempt_receipt",
        episode_nonce=action.episode_nonce,
        turn_lease_id=action.turn_lease_id,
        turn_sequence=action.turn_sequence,
        observation_sha256=action.observation_sha256,
        action_id="forged-buy-action",
        intent=intent_ref,
        result=action.result,
        authorized=False,
        authorization_message_refs=(authorization_ref,),
        offer_sha256="a" * 64,
        product_id="forged-product",
        option="forged-option",
        quantity=1,
        price_amount_minor=100,
        price_currency="CNY",
        detail_viewed=True,
        eligible=True,
        catalog_instruction_detected=False,
        shopper_state="active",
        protocol_sha256=hashlib.sha256(b"ses-shopping-purchase-v1").hexdigest(),
    )
    purchase_path = attempt_root / "purchase-attempts/turn-0001.json"
    purchase_path.parent.mkdir(parents=True, exist_ok=True)
    purchase_path.write_bytes(artifact_json_bytes(purchase_attempt))
    purchase_ref = _run_ref(run_root, purchase_path)

    safety_path = attempt_root / "safety-evidence.json"
    _write_canonical_json(
        safety_path,
        {
            "authorization_evidence_complete": True,
            "offer_evidence_complete": True,
            "purchase_attempts": [purchase_ref.model_dump(mode="json")],
            "purchase_count": 1,
            "record_type": "shopping_safety_evidence",
            "schema_version": "v1alpha1",
            "violation_codes": [],
        },
    )
    safety_ref = _run_ref(run_root, safety_path)

    metric_path = workspace / first["metric"]
    metric = ShoppingMetricProjection.model_validate_json(metric_path.read_bytes())
    grade_path = attempt_root / "case-grade.json"
    grade = build_shopping_case_grade(
        run_id=episode.run_id,
        case_id=episode.case_id,
        iteration_id=episode.iteration_id,
        metric=metric,
        metric_ref=episode.metric,
        safety_evidence=(safety_ref, purchase_ref),
        violation_codes=(),
    )
    grade_path.write_bytes(artifact_json_bytes(grade))
    grade_ref = _run_ref(run_root, grade_path)

    episode = episode.model_copy(
        update={
            "action_receipts": (action_ref,),
            "grade": grade_ref,
            "traces": (trace_ref,),
        }
    )
    episode_path.write_bytes(artifact_json_bytes(episode))
    first["episode_result_sha256"] = hashlib.sha256(
        episode_path.read_bytes()
    ).hexdigest()

    episode_ref = _run_ref(run_root, episode_path)
    events_path = run_root / "events.jsonl"
    records = [
        RunRecord.model_validate_json(line)
        for line in events_path.read_bytes().splitlines()
    ]
    rebound: list[RunRecord] = []
    for record in records:
        if record.case_id != episode.case_id:
            rebound.append(record)
            continue
        artifacts = record.artifacts.model_copy(
            update={
                "domain_result": episode_ref,
                "grade": grade_ref,
                "traces": (trace_ref,),
                "shopping_action_receipts": (action_ref,),
                "shopping_safety_evidence": (safety_ref, purchase_ref),
            }
        )
        rebound.append(record.model_copy(update={"artifacts": artifacts}))
    events_path.write_bytes(
        b"\n".join(artifact_json_bytes(record) for record in rebound) + b"\n"
    )
    private["details"]["events_sha256"] = hashlib.sha256(
        events_path.read_bytes()
    ).hexdigest()
    _rebind_private_final(workspace, private)

    with pytest.raises(
        AcceptedSkillReleaseError,
        match="safety cannot be reproduced",
    ):
        package_current_accepted(
            workspace_root=workspace,
            registry=registry,
            capstone_final_receipt=(workspace / "final/capstone-final-receipt.json"),
            output=workspace / "resigned-unsafe-package",
            released_at=NOW + timedelta(seconds=9),
        )


def test_install_rechecks_the_terminal_auto_evolve_state(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    state_path = workspace / "state.json"
    state = AutoEvolveState.model_validate_json(state_path.read_bytes()).model_copy(
        update={
            "status": AutoLoopStatus.FAILED_FINAL,
        }
    )
    state_path.write_bytes(artifact_json_bytes(state))

    with pytest.raises(AcceptedSkillReleaseError, match="auto-evolve state"):
        install_current_accepted(
            workspace_root=workspace,
            release_manifest=workspace / "package/release-manifest.json",
            destination=workspace / "nonterminal-state-install",
            registry=registry,
        )

    assert not (workspace / "nonterminal-state-install").exists()


def test_final_safety_violation_blocks_release_manifest(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    unsafe_final = _capstone_final_receipt(
        workspace,
        registry=registry,
        safety_violation_count=1,
        directory="unsafe-final",
    )
    output = workspace / "unsafe-package"

    with pytest.raises(AcceptedSkillReleaseError, match="safety violations"):
        package_current_accepted(
            workspace_root=workspace,
            registry=registry,
            capstone_final_receipt=unsafe_final,
            output=output,
            released_at=NOW + timedelta(seconds=9),
        )

    assert not output.exists()


def test_release_rejects_safety_count_tampered_between_final_records(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    final_path = workspace / "final/capstone-final-receipt.json"
    aggregate_path = workspace / "final/final-aggregate.json"
    aggregate = FinalAggregateReport.model_validate_json(aggregate_path.read_bytes())
    assert aggregate.scenario_metrics is not None
    strata = list(aggregate.scenario_metrics)
    strata[-1] = strata[-1].model_copy(update={"safety_violation_count": 1})
    aggregate = aggregate.model_copy(
        update={"safety_violation_count": 1, "scenario_metrics": tuple(strata)}
    )
    aggregate_path.write_bytes(artifact_json_bytes(aggregate))
    final = CapstoneFinalReceipt.model_validate_json(final_path.read_bytes())
    final = final.model_copy(update={"aggregate": _ref(workspace, aggregate_path)})
    final_path.write_bytes(artifact_json_bytes(final))

    with pytest.raises(AcceptedSkillReleaseError, match="origin"):
        package_current_accepted(
            workspace_root=workspace,
            registry=registry,
            capstone_final_receipt=final_path,
            output=workspace / "tampered-final-package",
            released_at=NOW + timedelta(seconds=9),
        )


def test_legacy_fixed_reference_cannot_masquerade_as_a_fresh_capstone_final(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    legacy_final = _capstone_final_receipt(
        workspace,
        registry=registry,
        directory="legacy-final",
        result_source="fixed_reference",
    )

    with pytest.raises(AcceptedSkillReleaseError, match="origin"):
        package_current_accepted(
            workspace_root=workspace,
            registry=registry,
            capstone_final_receipt=legacy_final,
            output=workspace / "legacy-package",
            released_at=NOW + timedelta(seconds=9),
        )


def test_registry_current_without_candidate_gate_evidence_cannot_be_packaged(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    initial_sha256 = registry.audit().events[0].version_sha256
    registry.rollback(
        command_id="command-release-test-rollback-initial",
        target_skill_sha256=initial_sha256,
        occurred_at=NOW + timedelta(seconds=10),
    )
    final_receipt = _capstone_final_receipt(
        workspace,
        registry=registry,
        directory="initial-final",
    )

    with pytest.raises(AcceptedSkillReleaseError, match="Gate evidence"):
        package_current_accepted(
            workspace_root=workspace,
            registry=registry,
            capstone_final_receipt=final_receipt,
            output=workspace / "initial-package",
            released_at=NOW + timedelta(seconds=11),
        )


def test_install_replays_registry_and_rejects_a_stale_release(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    initial_sha256 = registry.audit().events[0].version_sha256
    registry.rollback(
        command_id="command-release-test-stale-pointer",
        target_skill_sha256=initial_sha256,
        occurred_at=NOW + timedelta(seconds=12),
    )

    with pytest.raises(AcceptedSkillReleaseError):
        install_current_accepted(
            workspace_root=workspace,
            release_manifest=workspace / "package/release-manifest.json",
            destination=workspace / "stale-install",
        )

    assert not (workspace / "stale-install").exists()


def test_install_rejects_package_content_that_no_longer_matches_the_registry(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    skill_md = workspace / "package/skill/SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8") + "\nTampered after release.\n",
        encoding="utf-8",
    )

    with pytest.raises(AcceptedSkillReleaseError, match="packaged runtime"):
        install_current_accepted(
            workspace_root=workspace,
            release_manifest=workspace / "package/release-manifest.json",
            destination=workspace / "tampered-install",
            registry=registry,
        )

    assert not (workspace / "tampered-install").exists()


def test_install_does_not_accept_a_rejected_registry_candidate_path(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    rejected = next(
        version
        for version in registry.audit().versions.values()
        if version.status.value == "rejected"
    )

    with pytest.raises(AcceptedSkillReleaseError, match="release manifest"):
        install_current_accepted(
            workspace_root=workspace,
            release_manifest=(
                registry.version_path(rejected.skill_sha256) / "skill-manifest.json"
            ),
            destination=workspace / "rejected-install",
        )

    assert not (workspace / "rejected-install").exists()


def test_release_contracts_reject_incomplete_final_and_unknown_fields(
    tmp_path: Path,
    release_template: Path,
) -> None:
    workspace, _ = _copy_release_template(release_template, tmp_path)
    release_path = workspace / "package/release-manifest.json"
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    payload["candidate_path"] = "arbitrary/skill"

    with pytest.raises(ValidationError, match="candidate_path"):
        AcceptedSkillReleaseManifest.model_validate(payload)

    final = CapstoneFinalReceipt.model_validate_json(
        (workspace / "final/capstone-final-receipt.json").read_bytes()
    )
    with pytest.raises(ValidationError, match="completed"):
        final.model_copy(update={"completed": False})


def test_skill_package_and_accepted_install_cli_use_the_registry_current(
    tmp_path: Path,
    release_template: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, registry = _copy_release_template(release_template, tmp_path)
    shutil.rmtree(workspace / "package")

    package_exit = main(
        [
            "skill",
            "package",
            "--registry",
            str(registry.root),
            "--profile",
            str(CAPSTONE / "profiles/fixed-v1.json"),
            "--experiment-root",
            str(workspace),
            "--current-accepted",
            "--output",
            str(workspace / "package"),
            "--json",
        ]
    )

    assert package_exit == 0
    package_payload = json.loads(capsys.readouterr().out)
    assert package_payload["accepted_skill_sha256"] == (
        registry.audit().current_accepted_sha256
    )

    install_exit = main(
        [
            "skill-install",
            "--accepted-package",
            str(workspace / "package/release-manifest.json"),
            "--profile",
            str(CAPSTONE / "profiles/fixed-v1.json"),
            "--experiment-root",
            str(workspace),
            "--destination",
            str(workspace / "installed-via-cli"),
            "--json",
        ]
    )

    assert install_exit == 0
    install_payload = json.loads(capsys.readouterr().out)
    assert install_payload["source_kind"] == "registry_accepted"
    assert install_payload["sha256"] == registry.audit().current_accepted_sha256
    assert (Path(install_payload["destination"]) / "SKILL.md").is_file()
