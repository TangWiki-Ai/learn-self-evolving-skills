from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ses.automation.capstone import write_opaque_split_locks
from ses.contracts import (
    CandidateArtifact,
    EvidenceArtifact,
    EvidenceSource,
    FailureCardSet,
    FailureCategory,
    FailureEvidenceCase,
    FailureEvidenceFixture,
    FailureProvenance,
    JudgeSimulatorHealth,
    MeasurementKind,
    PairCategory,
    RunnerStatus,
    SchemaVersion,
    ShoppingFailureSubcode,
    artifact_json_bytes,
    normalized_files_sha256,
)
from ses.evolution.diagnosis import SHOPPING_DIAGNOSIS_POLICY
from ses.evolution.evidence import load_failure_evidence
from ses.evolution.gate import GateRequest, run_candidate_gate
from ses.evolution.updater import SHOPPING_UPDATER_POLICY, FixedShoppingUpdater
from ses.evolution.workflow import run_evolution_workflow
from ses.runner import BaselineRunner, BudgetLimits
from ses.shopping.course_workflow import (
    SHOPPING_STATIC_GATE_POLICY,
    run_shopping_create_stage,
    run_shopping_paired_stage,
    run_shopping_static_stage,
    run_shopping_trigger_stage,
)
from ses.shopping.fixed_course import build_fixed_develop_evaluation
from ses.shopping.gate import FixedShoppingGateAdapter, shopping_gate_policy
from ses.shopping.manual_workflow import run_shopping_evolution_stage
from ses.shopping.profile import load_shopping_profile, shopping_experiment_id
from ses.skills.installer import (
    load_skill_manifest,
    normalized_skill_sha256,
)
from ses.skills.static_gate import StaticGateStatus, run_static_gate

ROOT = Path(__file__).resolve().parents[2]
CAPSTONE = ROOT / "course" / "capstone-shopping-assistant"
MEASURED_AT = datetime(2026, 8, 20, tzinfo=UTC)
SHA = "a" * 64


def _evidence(path: Path, *, skill_sha256: str) -> Path:
    subcodes = (
        ShoppingFailureSubcode.CONSTRAINT_LOST,
        ShoppingFailureSubcode.MISSING_CRITICAL_QUESTION,
        ShoppingFailureSubcode.CLARIFIED_TOO_LATE,
        ShoppingFailureSubcode.UNAUTHORIZED_PURCHASE,
    )
    cases = []
    for index, subcode in enumerate(subcodes, 1):
        prefix = f"develop/case-{index:03d}"
        category = {
            ShoppingFailureSubcode.CONSTRAINT_LOST: FailureCategory.PATTERN,
            ShoppingFailureSubcode.MISSING_CRITICAL_QUESTION: FailureCategory.OVERLOAD,
            ShoppingFailureSubcode.CLARIFIED_TOO_LATE: FailureCategory.TIMING,
            ShoppingFailureSubcode.UNAUTHORIZED_PURCHASE: FailureCategory.SAFETY,
        }[subcode]
        cases.append(
            FailureEvidenceCase(
                case_key=f"case-{index:03d}",
                pair_category=PairCategory.PASS_TO_FAIL,
                baseline_status=RunnerStatus.PASS,
                skill_status=RunnerStatus.AGENT_FAIL,
                trace=EvidenceArtifact(
                    kind="trace", source_file=f"{prefix}/trace.json", sha256=SHA
                ),
                assertion=EvidenceArtifact(
                    kind="assertion",
                    source_file=f"{prefix}/assertion.json",
                    sha256=SHA,
                ),
                failure_categories=(category,),
                shopping_subcode=subcode,
                episode_evidence=EvidenceArtifact(
                    kind="episode", source_file=f"{prefix}/episode.json", sha256=SHA
                ),
                raw_reward_evidence=EvidenceArtifact(
                    kind="raw_reward",
                    source_file=f"{prefix}/raw-reward.json",
                    sha256=SHA,
                ),
                metric_evidence=EvidenceArtifact(
                    kind="metric", source_file=f"{prefix}/metric.json", sha256=SHA
                ),
                safety_evidence=(
                    EvidenceArtifact(
                        kind="safety",
                        source_file=f"{prefix}/safety.json",
                        sha256=SHA,
                    ),
                ),
                judge_simulator_health=JudgeSimulatorHealth.HEALTHY,
                observation="课程固定 develop 证据显示该行为需要修改 Skill。",
            )
        )
    fixture = FailureEvidenceFixture(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="failure_evidence_fixture",
        provenance=FailureProvenance.SYNTHETIC,
        source=EvidenceSource(
            source_label="shopping-fixed-reviewed-v1",
            comparison_sha256=SHA,
            pair_execution_sha256=SHA,
            baseline_events_sha256=SHA,
            skill_events_sha256=SHA,
            skill_sha256=skill_sha256,
            measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        ),
        cases=tuple(cases),
        redaction_notice=(
            "provider_streams_paths_gold_and_private_model_content_removed"
        ),
    )
    path.write_bytes(artifact_json_bytes(fixture))
    return path


def test_shopping_evolution_injects_domain_updater_and_static_policy(
    tmp_path: Path,
) -> None:
    profile = load_shopping_profile(CAPSTONE / "profiles" / "fixed-v1.json")
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE / "fixtures" / "creator-projections",
        experiment_root=tmp_path / "experiment",
    )
    output = tmp_path / "manual-evolution"

    run_evolution_workflow(
        parent_dir=created.skill_source,
        evidence_path=_evidence(
            tmp_path / "shopping-failure-evidence.json",
            skill_sha256=created.receipt.skill_sha256,
        ),
        output_root=output,
        updater=FixedShoppingUpdater(),
        mode="fixed",
        diagnosis_policy=SHOPPING_DIAGNOSIS_POLICY,
        updater_policy=SHOPPING_UPDATER_POLICY,
        static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
    )

    candidate = output / "skill"
    candidate_manifest = load_skill_manifest(candidate)
    assert candidate_manifest.name == "shopping-assistant"
    assert candidate_manifest.source_kind == "candidate"
    assert candidate_manifest.tool_protocol_sha256 == profile.profile.turn_policy_sha256
    assert (
        run_static_gate(candidate, policy=SHOPPING_STATIC_GATE_POLICY).status
        is StaticGateStatus.PASS
    )
    assert run_static_gate(candidate).status is StaticGateStatus.FAIL


def test_shopping_gate_uses_v1alpha2_and_the_same_eight_stages(
    tmp_path: Path,
) -> None:
    profile_path = CAPSTONE / "profiles" / "fixed-v1.json"
    profile = load_shopping_profile(profile_path)
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE / "fixtures" / "creator-projections",
        experiment_root=tmp_path / "experiment",
    )
    bundle = tmp_path / "manual-evolution"
    run_evolution_workflow(
        parent_dir=created.skill_source,
        evidence_path=_evidence(
            tmp_path / "shopping-failure-evidence.json",
            skill_sha256=created.receipt.skill_sha256,
        ),
        output_root=bundle,
        updater=FixedShoppingUpdater(),
        mode="fixed",
        diagnosis_policy=SHOPPING_DIAGNOSIS_POLICY,
        updater_policy=SHOPPING_UPDATER_POLICY,
        static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
    )
    experiment_id = shopping_experiment_id(profile)
    locks = write_opaque_split_locks(
        experiment_root=tmp_path / "gate-experiment",
        experiment_id=experiment_id,
        profile_sha256=profile.profile_sha256,
        mode="fixed",
        selection_case_count=8,
        selection_commitment_sha256=(
            profile.profile.protected_split_commitments["selection"]
        ),
        final_commitment_sha256=profile.profile.protected_split_commitments["final"],
        generated_at=MEASURED_AT,
    )
    policy = shopping_gate_policy(
        profile,
        selection_lock=locks.selection,
        experiment_id=experiment_id,
    )

    decision = run_candidate_gate(
        GateRequest(
            gate_id="gate-shopping-manual",
            lineage_id="lineage-shopping-fixed-test",
            workspace_root=tmp_path / "gate-workspace",
            accepted_skill=created.skill_source,
            candidate_bundle=bundle,
            selection_lock=locks.selection,
            policy=policy,
            static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
            mode="fixed",
            measured_at=MEASURED_AT,
        ),
        adapter=FixedShoppingGateAdapter("accept"),
    )

    assert decision.schema_version is SchemaVersion.V1ALPHA2
    assert decision.outcome.value == "accepted"
    assert len(decision.steps) == 8
    assert decision.metrics.candidate_full_success_count == 4
    assert decision.metrics.candidate_safety_violation_count == 0


def test_fixed_shopping_gate_trigger_reads_candidate_description(
    tmp_path: Path,
) -> None:
    profile = load_shopping_profile(CAPSTONE / "profiles" / "fixed-v1.json")
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE / "fixtures" / "creator-projections",
        experiment_root=tmp_path / "experiment",
    )
    bundle = tmp_path / "manual-evolution"
    run_evolution_workflow(
        parent_dir=created.skill_source,
        evidence_path=_evidence(
            tmp_path / "shopping-failure-evidence.json",
            skill_sha256=created.receipt.skill_sha256,
        ),
        output_root=bundle,
        updater=FixedShoppingUpdater(),
        mode="fixed",
        diagnosis_policy=SHOPPING_DIAGNOSIS_POLICY,
        updater_policy=SHOPPING_UPDATER_POLICY,
        static_gate_policy=SHOPPING_STATIC_GATE_POLICY,
    )
    candidate = CandidateArtifact.model_validate_json(
        (bundle / "candidate.json").read_bytes()
    )
    adapter = FixedShoppingGateAdapter("accept")
    healthy = adapter.run_trigger(
        candidate=candidate,
        skill_sha256=candidate.content_sha256,
        measured_at=MEASURED_AT,
    )
    assert (healthy.tp, healthy.fn, healthy.tn, healthy.fp) == (10, 0, 10, 0)

    files = dict(candidate.files)
    files["SKILL.md"] = files["SKILL.md"].replace(
        "description: 处理中文购买前商品搜索、约束核对、比较、澄清和明确授权后的购买。",
        "description: 处理通用请求。",
    )
    content_sha256 = normalized_files_sha256(files)
    manifest_files = tuple(
        item.model_copy(
            update={
                "sha256": hashlib.sha256(files[item.path].encode("utf-8")).hexdigest()
            }
        )
        for item in candidate.manifest.files
    )
    damaged = CandidateArtifact.model_validate(
        {
            **candidate.model_dump(mode="python"),
            "content_sha256": content_sha256,
            "files": files,
            "manifest": candidate.manifest.model_copy(
                update={
                    "content_sha256": content_sha256,
                    "files": manifest_files,
                }
            ),
        }
    )

    failed = adapter.run_trigger(
        candidate=damaged,
        skill_sha256=damaged.content_sha256,
        measured_at=MEASURED_AT,
    )
    assert failed.tp == 0
    assert failed.fn == 10
    assert failed.tn == 10
    assert failed.fp == 0


def test_fixed_develop_fixture_exposes_four_pair_categories_and_safety_failure(
    tmp_path: Path,
) -> None:
    profile = load_shopping_profile(CAPSTONE / "profiles" / "fixed-v1.json")
    experiment = tmp_path / "experiment"
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE / "fixtures" / "creator-projections",
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

    assert set(paired.comparison.category_counts.values()) == {3}
    assert paired.metrics.comparable_case_count == 12
    assert paired.metrics.baseline_full_success_count == 6
    assert paired.metrics.skill_full_success_count == 6
    assert paired.metrics.skill_safety_violation_count == 2

    evolved = run_shopping_evolution_stage(
        profile=profile,
        experiment_root=experiment,
        paired_receipt=paired.receipt_path,
    )
    fixture = load_failure_evidence(evolved.evidence_path)
    cards = FailureCardSet.model_validate_json(evolved.failure_cards_path.read_bytes())

    assert len(fixture.cases) == len(cards.cards) == 3
    assert {card.category for card in cards.cards} == {
        FailureCategory.PATTERN,
        FailureCategory.OVERLOAD,
        FailureCategory.SAFETY,
    }
    unauthorized = next(
        card
        for card in cards.cards
        if card.shopping_subcode is ShoppingFailureSubcode.UNAUTHORIZED_PURCHASE
    )
    assert unauthorized.safety_evidence
    assert evolved.summary.failure_card_count == 3
    assert evolved.summary.patch_operation_count == 3


def test_removing_constraint_seed_behavior_changes_fixed_develop_outcomes(
    tmp_path: Path,
) -> None:
    profile = load_shopping_profile(CAPSTONE / "profiles" / "fixed-v1.json")
    projection_root = CAPSTONE / "fixtures" / "creator-projections"
    changed_projections = tmp_path / "changed-projections"
    shutil.copytree(projection_root, changed_projections)
    for projection in changed_projections.glob("*.json"):
        content = projection.read_text(encoding="utf-8")
        projection.write_text(
            content.replace("硬约束", "可协商偏好"),
            encoding="utf-8",
        )
    original = run_shopping_create_stage(
        profile=profile,
        projection_root=projection_root,
        experiment_root=tmp_path / "original-experiment",
    )
    modified = run_shopping_create_stage(
        profile=profile,
        projection_root=changed_projections,
        experiment_root=tmp_path / "modified-experiment",
    )

    def statuses(source: Path, output: Path, run_id: str) -> dict[str, str]:
        fixed = build_fixed_develop_evaluation(
            profile,
            learner_skill_sha256=normalized_skill_sha256(source),
            learner_skill_source=source,
        )
        completed = BaselineRunner(output, fixed.skill_evaluator).run(
            run_id=run_id,
            case_ids=tuple(fixed.tasks),
            iterations=1,
            budgets=BudgetLimits(max_cases=12, max_turns_per_case=3),
            data_version=profile.profile_sha256,
            model_lock_hash=profile.profile.agent_model_sha256,
            skill_hash=normalized_skill_sha256(source),
            protocol_version="shopping-fixed-policy-test-v1",
        )
        return {
            case_id: str(completed.latest_results[(case_id, "iteration-0")]["status"])
            for case_id in fixed.tasks
        }

    original_statuses = statuses(
        original.skill_source,
        tmp_path / "original-run",
        "run-shopping-original-policy",
    )
    modified_statuses = statuses(
        modified.skill_source,
        tmp_path / "modified-run",
        "run-shopping-modified-policy",
    )

    assert original_statuses != modified_statuses
    assert {
        case_id
        for case_id in original_statuses
        if original_statuses[case_id] != modified_statuses[case_id]
    }
