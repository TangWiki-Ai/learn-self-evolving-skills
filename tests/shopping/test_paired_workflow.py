from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from ses.contracts import SchemaVersion, Trace
from ses.contracts.runner import RunnerStatus
from ses.contracts.shopping import (
    EpisodeStart,
    EpisodeStep,
    MeasurementLevel,
    RawShopSimulatorReward,
    ShoppingActionKind,
    ShoppingObservation,
    ShoppingScenario,
    ShoppingTaskRef,
    ShopSimulatorEpisodeResult,
)
from ses.runner import CaseEvaluation
from ses.runner.baseline import AttemptEvaluator, EvaluationContext
from ses.shopping.adapters import InMemoryEpisodeFixture, InMemoryShopSimulatorAdapter
from ses.shopping.course_workflow import (
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
    ShoppingSplit,
    expand_source_groups,
    load_shopping_profile,
    public_task_refs,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CAPSTONE_ROOT = PROJECT_ROOT / "course" / "capstone-shopping-assistant"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _develop_tasks(
    profile: LoadedShoppingProfile,
) -> dict[str, ShoppingTaskRef]:
    split_counts: tuple[tuple[ShoppingSplit, int], ...] = (
        ("creator", 2),
        ("develop", 3),
        ("selection", 2),
        ("final", 3),
    )
    groups = tuple(
        ShoppingSourceGroup(
            source_group_id=f"private-{split}-{index:02d}",
            semantic_family_id=f"family-{split}-{index:02d}",
            split=split,
        )
        for split, count in split_counts
        for index in range(1, count + 1)
    )
    slots = expand_source_groups(profile.profile, groups)
    refs = tuple(task for task in public_task_refs(slots) if task.split == "develop")
    return {f"shopping-develop-{index:02d}": task for index, task in enumerate(refs, 1)}


def _adapter(
    tasks: Mapping[str, ShoppingTaskRef],
) -> InMemoryShopSimulatorAdapter:
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
                        reward=Decimal("1"),
                        r_type=Decimal("1"),
                        r_att=Decimal("1"),
                        r_option=Decimal("1"),
                        r_price=Decimal("1"),
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
        measurement_level=MeasurementLevel.SYNTHETIC_OFFLINE,
        model_lock_sha256=profile.profile.agent_model_sha256,
        skill_sha256=skill_sha256,
        protocol_sha256=profile.profile.turn_policy_sha256,
    )


class _OneInfrastructureFailure:
    def __init__(self, delegate: AttemptEvaluator, *, case_id: str) -> None:
        self._delegate = delegate
        self._case_id = case_id

    def evaluate_attempt(self, context: EvaluationContext) -> CaseEvaluation:
        if context.case_id == self._case_id:
            return CaseEvaluation(
                case_id=context.case_id,
                iteration_id=context.iteration_id,
                status=RunnerStatus.INFRASTRUCTURE_ERROR,
                turn_count=0,
                input_tokens=0,
                output_tokens=0,
                error="fixed infrastructure fixture",
            )
        return self._delegate.evaluate_attempt(context)


def test_fresh_shopping_pair_reuses_runner_and_binds_v1alpha2_metrics(
    tmp_path: Path,
) -> None:
    profile = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")
    experiment_root = tmp_path / "experiment"
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE_ROOT / "fixtures" / "creator-projections",
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
    tasks = _develop_tasks(profile)
    port = _adapter(tasks)

    result = run_shopping_paired_stage(
        profile=profile,
        experiment_root=experiment_root,
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

    assert result.comparison.schema_version is SchemaVersion.V1ALPHA2
    assert len(result.comparison.cases) == 12
    assert (
        result.metrics.pair_execution_sha256 == result.comparison.pair_execution_sha256
    )
    assert result.metrics.comparable_case_count == 12
    assert Counter(row.scenario for row in result.metrics.strata) == Counter(
        ShoppingScenario
    )
    assert {row.case_count for row in result.metrics.strata} == {3}
    assert result.metrics.cost_delta_amount == Decimal(0)
    assert result.receipt.stage == "paired"
    assert result.receipt.primary_metrics["paired_case_count"] == 12

    root = experiment_root
    for row in result.comparison.cases:
        assert row.baseline_domain_result is not None
        assert row.skill_domain_result is not None
        baseline_result = ShopSimulatorEpisodeResult.model_validate_json(
            (root / row.baseline_domain_result.path).read_bytes()
        )
        skill_result = ShopSimulatorEpisodeResult.model_validate_json(
            (root / row.skill_domain_result.path).read_bytes()
        )
        assert (
            baseline_result.profile_sha256
            == skill_result.profile_sha256
            == profile.profile_sha256
        )
        assert baseline_result.episode_nonce != skill_result.episode_nonce
        assert row.baseline_trace is not None and row.skill_trace is not None
        baseline_trace = Trace.model_validate_json(
            (root / row.baseline_trace.path).read_bytes()
        )
        skill_trace = Trace.model_validate_json(
            (root / row.skill_trace.path).read_bytes()
        )
        assert baseline_trace.trace_id != skill_trace.trace_id
        assert baseline_trace.session_id != skill_trace.session_id


def test_shopping_pair_excludes_infrastructure_error_from_comparable_denominator(
    tmp_path: Path,
) -> None:
    profile = load_shopping_profile(CAPSTONE_ROOT / "profiles" / "fixed-v1.json")
    experiment_root = tmp_path / "experiment"
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE_ROOT / "fixtures" / "creator-projections",
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
    tasks = _develop_tasks(profile)
    failed_case = next(iter(tasks))
    failed_scenario = tasks[failed_case].scenario
    port = _adapter(tasks)
    skill_evaluator = _evaluator(
        port=port,
        tasks=tasks,
        profile=profile,
        skill_sha256=created.receipt.skill_sha256,
    )

    result = run_shopping_paired_stage(
        profile=profile,
        experiment_root=experiment_root,
        skill_source=created.skill_source,
        trigger_receipt=trigger.receipt_path,
        tasks=tasks,
        baseline_evaluator=_evaluator(
            port=port,
            tasks=tasks,
            profile=profile,
            skill_sha256=EMPTY_SHA256,
        ),
        skill_evaluator=_OneInfrastructureFailure(
            skill_evaluator,
            case_id=failed_case,
        ),
    )

    failed_row = next(
        row for row in result.comparison.cases if row.case_id == failed_case
    )
    failed_stratum = next(
        row for row in result.metrics.strata if row.scenario is failed_scenario
    )
    assert failed_row.skill_status is RunnerStatus.INFRASTRUCTURE_ERROR
    assert failed_row.comparable is False
    assert result.metrics.case_count == 12
    assert result.metrics.comparable_case_count == 11
    assert failed_stratum.case_count == 3
    assert failed_stratum.comparable_case_count == 2
    assert result.comparison.baseline_pass_rate == 1.0
    assert result.comparison.skill_pass_rate == 1.0
