from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ses.automation.capstone import (
    CapstoneIndexError,
    _manual_branch_events,
    write_opaque_split_locks,
)
from ses.automation.fixed import (
    FixedFinalAdapter,
    FixedRolloutAdapter,
    build_fixed_auto_evolve_orchestrator,
)
from ses.automation.orchestrator import AutoEvolveError, AutoEvolveOrchestrator
from ses.contracts import (
    AutoLoopStatus,
    AutoRolloutReceipt,
    CapstoneFinalReceipt,
    FailureCardSet,
    FinalAggregateReport,
    FinalLifecycle,
    GateDecision,
    GateOutcome,
    OpaqueProtectedSplitLock,
    RegistryEventType,
    SchemaVersion,
    SelectionPairEvaluation,
    SplitLockFormat,
    artifact_json_bytes,
    content_sha256,
)
from ses.contracts.shopping import ShoppingScenario, ShopSimulatorEpisodeResult
from ses.evolution.gate import default_gate_policy
from ses.reporting.l3 import load_l3_inputs, render_l3_html
from ses.shopping.automation import build_shopping_capstone_orchestrator
from ses.shopping.course_workflow import (
    SHOPPING_STATIC_GATE_POLICY,
    run_shopping_create_stage,
    run_shopping_paired_stage,
    run_shopping_static_stage,
    run_shopping_trigger_stage,
)
from ses.shopping.fixed_course import build_fixed_develop_evaluation
from ses.shopping.manual_workflow import (
    promote_shopping_candidate,
    register_shopping_candidate,
    run_shopping_evolution_stage,
    run_shopping_gate_stage,
)
from ses.shopping.profile import load_shopping_profile
from ses.shopping.registry import open_shopping_registry
from ses.skills.static_gate import StaticGateStatus, run_static_gate

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_SHA256 = hashlib.sha256(b"shopping-fixed-v1-profile").hexdigest()
FAILURE_FIXTURE = (
    PROJECT_ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
)
CAPSTONE = PROJECT_ROOT / "course/capstone-shopping-assistant"


def _orchestrator(
    output: Path,
    final: FixedFinalAdapter,
    *,
    profile_sha256: str = PROFILE_SHA256,
) -> AutoEvolveOrchestrator:
    locks = write_opaque_split_locks(
        experiment_root=output,
        experiment_id="experiment-shopping-capstone-auto",
        profile_sha256=profile_sha256,
        mode="fixed",
        selection_case_count=6,
        selection_commitment_sha256="1" * 64,
        final_commitment_sha256="2" * 64,
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    legacy_lock = PROJECT_ROOT / "data/testset/protected/selection-manifest.json"
    policy = default_gate_policy(PROJECT_ROOT, legacy_lock).model_copy(
        update={
            "selection_lock_sha256": hashlib.sha256(
                locks.selection.read_bytes()
            ).hexdigest()
        }
    )
    return build_fixed_auto_evolve_orchestrator(
        project_root=PROJECT_ROOT,
        output_root=output,
        experiment_id="experiment-shopping-capstone-auto",
        final_lifecycle=FinalLifecycle.INDEPENDENT_CAPSTONE,
        profile_sha256=profile_sha256,
        selection_lock=locks.selection,
        final_lock=locks.final,
        split_lock_format=SplitLockFormat.CONTENT_ADDRESSED,
        gate_policy=policy,
        rollout_adapter=FixedRolloutAdapter(
            FAILURE_FIXTURE,
            source_kind="fresh_fixed_execution",
        ),
        final_adapter=final,
    )


def test_capstone_config_hash_includes_nonempty_protocol_locks(
    tmp_path: Path,
) -> None:
    config = _orchestrator(
        tmp_path / "experiment",
        FixedFinalAdapter(result_source="fresh_fixed_execution"),
    ).command.config
    artifact = artifact_json_bytes(config)
    wire = json.loads(artifact)

    assert wire["final_lifecycle"] == "independent_capstone"
    assert wire["profile_sha256"] == PROFILE_SHA256
    assert wire["split_lock_format"] == "content_addressed"
    assert content_sha256(config) == hashlib.sha256(artifact).hexdigest()

    changed_profile = config.model_copy(update={"profile_sha256": "f" * 64})
    assert content_sha256(changed_profile) != content_sha256(config)


def test_shopping_auto_evolve_stops_before_independent_final(tmp_path: Path) -> None:
    final = FixedFinalAdapter(result_source="fresh_fixed_execution")
    orchestrator = _orchestrator(tmp_path / "experiment", final)

    state = orchestrator.run()

    assert state.status is AutoLoopStatus.STOPPED
    assert state.completed_rounds == 2
    assert [row.gate_outcome for row in state.rounds] == [
        GateOutcome.ACCEPTED,
        GateOutcome.REJECTED,
    ]
    assert state.current_accepted_skill_sha256 == state.rounds[0].candidate_skill_sha256
    assert final.calls == 0
    assert not (tmp_path / "experiment/final").exists()
    selection_lock = OpaqueProtectedSplitLock.model_validate_json(
        (tmp_path / "experiment/protected/selection-lock.json").read_bytes()
    )
    final_lock = OpaqueProtectedSplitLock.model_validate_json(
        (tmp_path / "experiment/protected/final-lock.json").read_bytes()
    )
    assert selection_lock.case_count == 6
    assert final_lock.case_count == 12
    assert selection_lock.aggregate_commitment_sha256 != (
        final_lock.aggregate_commitment_sha256
    )
    assert {
        AutoRolloutReceipt.model_validate_json(
            (tmp_path / "experiment" / row.rollout.path).read_bytes()
        ).source_kind
        for row in state.rounds
    } == {"fresh_fixed_execution"}


def test_independent_final_is_fresh_and_idempotent(tmp_path: Path) -> None:
    output = tmp_path / "experiment"
    final = FixedFinalAdapter(result_source="fresh_fixed_execution")
    orchestrator = _orchestrator(output, final)
    stopped = orchestrator.run()

    completed = orchestrator.run_final_once()
    receipt_bytes = (output / "final/capstone-final-receipt.json").read_bytes()
    resumed = orchestrator.run_final_once()

    assert stopped.status is AutoLoopStatus.STOPPED
    assert completed.status is AutoLoopStatus.FINAL_COMPLETE
    assert resumed == completed
    assert final.calls == 1
    assert (output / "final/capstone-final-receipt.json").read_bytes() == receipt_bytes
    report = FinalAggregateReport.model_validate_json(
        (output / "final/final-aggregate.json").read_bytes()
    )
    receipt = CapstoneFinalReceipt.model_validate_json(receipt_bytes)
    assert report.case_count == 12
    assert report.schema_version is SchemaVersion.V1ALPHA2
    assert report.result_source == "fresh_fixed_execution"
    assert report.full_success_count == 10
    assert report.mean_strict_reward == Decimal("0.75")
    assert report.safety_violation_count == 0
    assert report.scenario_metrics is not None
    assert [row.case_count for row in report.scenario_metrics] == [3, 3, 3, 3]
    assert receipt.subject_skill_sha256 == completed.current_accepted_skill_sha256
    assert receipt.profile_sha256 == PROFILE_SHA256
    assert receipt.safety_violation_count == 0
    assert b"case_passes" not in receipt_bytes

    with pytest.raises(CapstoneIndexError, match="changed on resume"):
        _orchestrator(
            output,
            final,
            profile_sha256=hashlib.sha256(b"another-shopping-profile").hexdigest(),
        )


def test_final_safety_violation_records_failed_final_without_public_case_feedback(
    tmp_path: Path,
) -> None:
    output = tmp_path / "experiment"
    final = FixedFinalAdapter(
        result_source="fresh_fixed_execution",
        safety_violation_count=1,
    )
    orchestrator = _orchestrator(output, final)
    orchestrator.run()

    failed = orchestrator.run_final_once()
    receipt_bytes = (output / "final/capstone-final-receipt.json").read_bytes()
    repeated = orchestrator.run_final_once()

    assert failed.status is AutoLoopStatus.FAILED_FINAL
    assert repeated == failed
    assert final.calls == 1
    receipt = CapstoneFinalReceipt.model_validate_json(receipt_bytes)
    assert receipt.safety_violation_count == 1
    report = FinalAggregateReport.model_validate_json(
        (output / "final/final-aggregate.json").read_bytes()
    )
    assert report.safety_violation_count == receipt.safety_violation_count
    assert report.scenario_metrics is not None
    assert sum(row.safety_violation_count for row in report.scenario_metrics) == 1
    assert b"case_passes" not in receipt_bytes
    assert b"details" not in receipt_bytes
    rendered = render_l3_html(load_l3_inputs(output))
    assert "failed_final" in rendered
    assert "case_passes" not in rendered


def test_final_revalidates_opaque_split_locks_before_adapter_call(
    tmp_path: Path,
) -> None:
    output = tmp_path / "experiment"
    final = FixedFinalAdapter(result_source="fresh_fixed_execution")
    orchestrator = _orchestrator(output, final)
    stopped = orchestrator.run()
    lock_path = output / "protected/final-lock.json"
    tampered = json.loads(lock_path.read_text(encoding="utf-8"))
    tampered["aggregate_commitment_sha256"] = "3" * 64
    lock_path.write_text(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(AutoEvolveError, match="final lock differs"):
        orchestrator.run_final_once()

    assert stopped.status is AutoLoopStatus.STOPPED
    assert final.calls == 0
    assert not (output / "final").exists()


def test_opaque_split_lock_resume_rejects_fixed_live_reuse(tmp_path: Path) -> None:
    root = (tmp_path / "experiment").resolve()
    first = write_opaque_split_locks(
        experiment_root=root,
        experiment_id="experiment-shopping-capstone-auto",
        profile_sha256=PROFILE_SHA256,
        mode="fixed",
        selection_case_count=8,
        selection_commitment_sha256="1" * 64,
        final_commitment_sha256="2" * 64,
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    resumed = write_opaque_split_locks(
        experiment_root=root,
        experiment_id="experiment-shopping-capstone-auto",
        profile_sha256=PROFILE_SHA256,
        mode="fixed",
        selection_case_count=8,
        selection_commitment_sha256="1" * 64,
        final_commitment_sha256="2" * 64,
        generated_at=datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert resumed == first
    with pytest.raises(CapstoneIndexError, match="changed on resume"):
        write_opaque_split_locks(
            experiment_root=root,
            experiment_id="experiment-shopping-capstone-auto",
            profile_sha256=PROFILE_SHA256,
            mode="live",
            selection_case_count=8,
            selection_commitment_sha256="1" * 64,
            final_commitment_sha256="2" * 64,
            generated_at=datetime(2026, 8, 20, tzinfo=UTC),
        )


def test_shopping_builder_runs_two_domain_specific_auto_rounds(
    tmp_path: Path,
) -> None:
    profile = load_shopping_profile(CAPSTONE / "profiles/fixed-v1.json")
    experiment = (tmp_path / "experiment").resolve()
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE / "fixtures/creator-projections",
        experiment_root=experiment,
    )
    static = run_shopping_static_stage(
        profile=profile,
        experiment_root=experiment,
        skill_source=created.skill_source,
        create_receipt=created.receipt_path,
    )
    trigger = run_shopping_trigger_stage(
        profile=profile,
        experiment_root=experiment,
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
        experiment_root=experiment,
        skill_source=created.skill_source,
        trigger_receipt=trigger.receipt_path,
        tasks=fixed.tasks,
        baseline_evaluator=fixed.baseline_evaluator,
        skill_evaluator=fixed.skill_evaluator,
    )
    evolved = run_shopping_evolution_stage(
        profile=profile,
        experiment_root=experiment,
        paired_receipt=paired.receipt_path,
    )
    registry = open_shopping_registry(experiment / "registry")
    registry.initialize(
        command_id="command-shopping-initialize",
        accepted_skill=created.skill_source,
        evidence_paths=(experiment / "v0-pipeline-summary.json",),
        occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
        lineage_id=f"lineage-shopping-fixed-{profile.profile_sha256[:16]}",
    )
    register_shopping_candidate(
        registry_root=registry.root,
        candidate_bundle=evolved.candidate_bundle,
    )
    manual = run_shopping_gate_stage(
        profile=profile,
        experiment_root=experiment,
        registry_root=registry.root,
        candidate_bundle=evolved.candidate_bundle,
    )
    promote_shopping_candidate(
        registry_root=registry.root,
        decision_path=manual.decision_path,
        candidate_id=manual.decision.candidate_id,
    )
    promoted_state = open_shopping_registry(experiment / "registry").audit()
    manual_branches = _manual_branch_events(
        promoted_state.events,
        candidate_skill_sha256=manual.decision.candidate_skill_sha256,
        gate_decision_sha256=hashlib.sha256(
            manual.decision_path.read_bytes()
        ).hexdigest(),
    )
    assert [event.event_type for event in manual_branches] == [
        RegistryEventType.CANDIDATE_ACCEPTED
    ]
    assert any(
        event.event_type is RegistryEventType.PROMOTED
        and event.version_sha256 == manual.decision.candidate_skill_sha256
        for event in promoted_state.events
    )

    orchestrator = build_shopping_capstone_orchestrator(
        profile=profile,
        project_root=PROJECT_ROOT,
        experiment_root=experiment,
    )
    state = orchestrator.run()
    shopping_selection = OpaqueProtectedSplitLock.model_validate_json(
        (experiment / "protected/selection-lock.json").read_bytes()
    )

    assert state.status is AutoLoopStatus.STOPPED
    assert shopping_selection.case_count == 8
    assert [row.gate_outcome for row in state.rounds] == [
        GateOutcome.ACCEPTED,
        GateOutcome.REJECTED,
    ]
    assert all(
        run_static_gate(
            open_shopping_registry(experiment / "registry").version_path(
                row.candidate_skill_sha256
            ),
            policy=SHOPPING_STATIC_GATE_POLICY,
        ).status
        is StaticGateStatus.PASS
        for row in state.rounds
    )
    assert all(
        "fixed shopping round"
        in (
            open_shopping_registry(experiment / "registry")
            .version_path(row.candidate_skill_sha256)
            .joinpath("SKILL.md")
            .read_text(encoding="utf-8")
        )
        for row in state.rounds
    )
    assert all(
        GateDecision.model_validate_json(
            (experiment / row.gate_decision.path).read_bytes()
        ).schema_version
        is SchemaVersion.V1ALPHA2
        for row in state.rounds
    )
    assert all(
        all(
            card.shopping_subcode is not None
            for card in FailureCardSet.model_validate_json(
                (
                    experiment
                    / "rounds"
                    / f"round-{row.round_number:03d}"
                    / "reflection.json"
                ).read_bytes()
            ).cards
        )
        for row in state.rounds
    )
    gate_decisions = (
        manual.decision,
        *(
            GateDecision.model_validate_json(
                (experiment / row.gate_decision.path).read_bytes()
            )
            for row in state.rounds
        ),
    )
    for decision in gate_decisions:
        private_runs = experiment / "protected" / "selection-runs" / decision.gate_id
        accepted_results = tuple(
            sorted(
                private_runs.glob(
                    "run-*-accepted-shopping/artifacts/*/iteration-0/"
                    "attempt-0/episode-result.json"
                )
            )
        )
        candidate_results = tuple(
            sorted(
                private_runs.glob(
                    "run-*-candidate-shopping/artifacts/*/iteration-0/"
                    "attempt-0/episode-result.json"
                )
            )
        )
        assert len(accepted_results) == len(candidate_results) == 8
        accepted_episodes = tuple(
            ShopSimulatorEpisodeResult.model_validate_json(path.read_bytes())
            for path in accepted_results
        )
        candidate_episodes = tuple(
            ShopSimulatorEpisodeResult.model_validate_json(path.read_bytes())
            for path in candidate_results
        )
        assert {row.skill_sha256 for row in accepted_episodes} == {
            decision.accepted_skill_sha256
        }
        assert {row.skill_sha256 for row in candidate_episodes} == {
            decision.candidate_skill_sha256
        }
        assert {row.episode_nonce for row in accepted_episodes}.isdisjoint(
            row.episode_nonce for row in candidate_episodes
        )
        pair = SelectionPairEvaluation.model_validate_json(
            (
                experiment
                / "registry"
                / "gates"
                / decision.gate_id
                / "private"
                / "selection-pair.json"
            ).read_bytes()
        )
        assert pair.accepted_run_id == accepted_episodes[0].run_id
        assert pair.candidate_run_id == candidate_episodes[0].run_id
    completed = orchestrator.run_final_once()
    shopping_l3 = load_l3_inputs(
        experiment,
        registry=open_shopping_registry(experiment / "registry"),
    )
    assert completed.status is AutoLoopStatus.FINAL_COMPLETE
    assert shopping_l3.state.current_accepted_skill_sha256 == (
        completed.current_accepted_skill_sha256
    )
    assert shopping_l3.final_report is not None
    assert shopping_l3.final_report.schema_version is SchemaVersion.V1ALPHA2
    final_results = tuple(
        sorted(
            experiment.glob(
                "final/protected-evaluation/run-shopping-final-current-fixed/"
                "artifacts/*/iteration-0/attempt-0/episode-result.json"
            )
        )
    )
    assert len(final_results) == 12
    final_episodes = tuple(
        ShopSimulatorEpisodeResult.model_validate_json(path.read_bytes())
        for path in final_results
    )
    assert {row.skill_sha256 for row in final_episodes} == {
        completed.current_accepted_skill_sha256
    }
    assert Counter(row.scenario for row in final_episodes) == Counter(
        {scenario: 3 for scenario in ShoppingScenario}
    )
    assert shopping_l3.final_report.safety_violation_count == sum(
        row.safety_violation_count for row in final_episodes
    )
    private_final = json.loads(
        (experiment / "final/private-results.json").read_text(encoding="utf-8")
    )
    assert len(private_final["details"]["episode_results"]) == 12
    public_final = (experiment / "final/capstone-final-receipt.json").read_bytes()
    assert b"episode_results" not in public_final
