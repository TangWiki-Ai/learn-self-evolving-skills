"""Shared original fixed shopping pipeline fixture for cross-module tests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    MeasurementKind,
    SchemaVersion,
    SkillV0PipelineSummary,
    artifact_json_bytes,
)
from ses.contracts.shopping import (
    EpisodeStart,
    EpisodeStep,
    RawShopSimulatorReward,
    ShoppingActionKind,
    ShoppingObservation,
    ShoppingTaskRef,
)
from ses.reporting.l2 import write_l2_html
from ses.shopping.adapters import InMemoryEpisodeFixture, InMemoryShopSimulatorAdapter
from ses.shopping.course_workflow import (
    ShoppingPairedStageResult,
    ShoppingTriggerStageResult,
    run_shopping_create_stage,
    run_shopping_paired_stage,
    run_shopping_static_stage,
    run_shopping_trigger_stage,
)
from ses.shopping.evaluator import ShopSimulatorAttemptEvaluator
from ses.shopping.fixed_engine import ScriptedShoppingEngine, ScriptedShoppingTurn
from ses.shopping.profile import (
    LoadedShoppingProfile,
    ShoppingSourceGroup,
    expand_source_groups,
    load_shopping_profile,
    public_task_refs,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPSTONE_ROOT = PROJECT_ROOT / "fixtures" / "seed" / "capstone-shopping-assistant"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class FixedV0Pipeline:
    root: Path
    profile: LoadedShoppingProfile
    skill_source: Path
    trigger: ShoppingTriggerStageResult
    paired: ShoppingPairedStageResult
    summary_path: Path


def _develop_tasks(profile: LoadedShoppingProfile) -> dict[str, ShoppingTaskRef]:
    groups = tuple(
        ShoppingSourceGroup(
            source_group_id=f"private-{split}-{index:02d}",
            semantic_family_id=f"family-{split}-{index:02d}",
            split=split,  # type: ignore[arg-type]
        )
        for split, count in (
            ("creator", 2),
            ("develop", 3),
            ("selection", 2),
            ("final", 3),
        )
        for index in range(1, count + 1)
    )
    refs = tuple(
        task
        for task in public_task_refs(expand_source_groups(profile.profile, groups))
        if task.split == "develop"
    )
    return {f"shopping-develop-{index:02d}": task for index, task in enumerate(refs, 1)}


def _adapter(tasks: Mapping[str, ShoppingTaskRef]) -> InMemoryShopSimulatorAdapter:
    fixtures = {}
    for task in tasks.values():
        nonce = f"fixture-{task.opaque_slot}"
        fixtures[task.opaque_slot] = InMemoryEpisodeFixture(
            start=EpisodeStart(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="shopping_episode_start",
                episode_nonce=nonce,
                task_slot=task.opaque_slot,
                scenario=task.scenario,
                sequence=0,
                observation=ShoppingObservation(text="搜索商品", allows_search=True),
                terminal=False,
                initial_authorization=False,
            ),
            steps=(
                EpisodeStep(
                    schema_version=SchemaVersion.V1ALPHA1,
                    record_type="shopping_episode_step",
                    episode_nonce=nonce,
                    sequence=1,
                    observation=ShoppingObservation(text="完成"),
                    terminal=True,
                    terminal_reason="upstream_terminal",
                    raw_reward=RawShopSimulatorReward(
                        schema_version=SchemaVersion.V1ALPHA1,
                        record_type="raw_shop_simulator_reward",
                        reward=Decimal(1),
                        r_type=Decimal(1),
                        r_att=Decimal(1),
                        r_option=Decimal(1),
                        r_price=Decimal(1),
                        source_names=("reward", "reward_detail"),
                    ),
                ),
            ),
        )
    return InMemoryShopSimulatorAdapter(fixtures)


def _evaluator(
    *,
    port: InMemoryShopSimulatorAdapter,
    tasks: Mapping[str, ShoppingTaskRef],
    profile: LoadedShoppingProfile,
    skill_sha256: str,
) -> ShopSimulatorAttemptEvaluator:
    return ShopSimulatorAttemptEvaluator(
        port=port,
        tasks=tasks,
        engine_factory=lambda _context: ScriptedShoppingEngine(
            (ScriptedShoppingTurn(ShoppingActionKind.SEARCH, "耳机"),)
        ),
        profile_sha256=profile.profile_sha256,
        measurement_level=profile.profile.measurement_level,
        model_lock_sha256=profile.profile.agent_model_sha256,
        skill_sha256=skill_sha256,
        protocol_sha256=profile.profile.turn_policy_sha256,
    )


def _ref(root: Path, path: Path) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def build_fixed_v0_pipeline(tmp_path: Path) -> FixedV0Pipeline:
    root = tmp_path / "shopping-pipeline"
    profile = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE_ROOT / "fixtures" / "creator-projections",
        experiment_root=root,
    )
    static = run_shopping_static_stage(
        profile=profile,
        experiment_root=root,
        skill_source=created.skill_source,
        create_receipt=created.receipt_path,
    )
    trigger = run_shopping_trigger_stage(
        profile=profile,
        experiment_root=root,
        skill_source=created.skill_source,
        static_receipt=static.receipt_path,
    )
    tasks = _develop_tasks(profile)
    port = _adapter(tasks)
    paired = run_shopping_paired_stage(
        profile=profile,
        experiment_root=root,
        skill_source=created.skill_source,
        trigger_receipt=trigger.receipt_path,
        tasks=tasks,
        baseline_evaluator=_evaluator(
            port=port,
            tasks=tasks,
            profile=profile,
            skill_sha256=EMPTY_SHA256,
        ),
        skill_evaluator=_evaluator(
            port=port,
            tasks=tasks,
            profile=profile,
            skill_sha256=created.receipt.skill_sha256,
        ),
    )
    l2_path = root / "l2.html"
    write_l2_html(
        paired.comparison,
        trigger.evaluation,
        l2_path,
        artifact_root=root,
    )
    summary = SkillV0PipelineSummary(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_v0_pipeline_summary",
        mode="fixed",
        seed_count=8,
        seed_review_status="course_original_reviewed",
        skill_sha256=created.receipt.skill_sha256,
        creator_measurement=MeasurementKind(profile.profile.measurement_level.value),
        trigger_measurement=trigger.evaluation.measurement_kind,
        paired_measurement=paired.comparison.measurement_kind,
        static_gate="pass",
        trigger_precision=trigger.evaluation.precision,
        trigger_recall=trigger.evaluation.recall,
        paired_case_count=len(paired.comparison.cases),
        baseline_pass_rate=paired.comparison.baseline_pass_rate,
        skill_pass_rate=paired.comparison.skill_pass_rate,
        static_gate_result=_ref(root, root / "static-gate.json"),
        trigger_result=_ref(root, root / "trigger-eval.json"),
        paired_comparison=_ref(root, paired.comparison_path),
        l2_html=_ref(root, l2_path),
    )
    summary_path = root / "summary.json"
    summary_path.write_bytes(artifact_json_bytes(summary))
    return FixedV0Pipeline(
        root=root,
        profile=profile,
        skill_source=created.skill_source,
        trigger=trigger,
        paired=paired,
        summary_path=summary_path,
    )
