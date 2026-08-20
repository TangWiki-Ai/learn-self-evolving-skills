"""Original deterministic fixtures for the fixed shopping capstone route."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from ses.contracts import SchemaVersion
from ses.contracts.shopping import (
    EpisodeStart,
    EpisodeStep,
    MeasurementLevel,
    RawShopSimulatorReward,
    ShoppingAction,
    ShoppingActionKind,
    ShoppingAvailableAction,
    ShoppingObservation,
    ShoppingPurchaseOffer,
    ShoppingTaskRef,
)
from ses.shopping.adapters import (
    InMemoryActionTransition,
    InMemoryEpisodeFixture,
    InMemoryShopSimulatorAdapter,
)
from ses.shopping.evaluator import ShopSimulatorAttemptEvaluator
from ses.shopping.fixed_engine import (
    FIXED_BROAD_QUERY,
    FIXED_CONSTRAINT_QUERY,
    FIXED_CUE_CONSTRAINT_SEARCH,
    FIXED_CUE_FAREWELL,
    FIXED_CUE_NO_ELIGIBLE,
    FIXED_CUE_PREFERENCE_EXPLORE,
    FIXED_CUE_STANDARD_SEARCH,
    FixedShoppingPolicyEngine,
    FixedShoppingSkillPolicy,
    ScriptedShoppingEngine,
    ScriptedShoppingTurn,
)
from ses.shopping.profile import (
    LoadedShoppingProfile,
    ShoppingSourceGroup,
    expand_public_source_groups,
    public_task_refs,
)
from ses.shopping.safety import ShoppingPrivateExpectation

_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@dataclass(frozen=True, slots=True)
class FixedDevelopEvaluation:
    """Fresh pair inputs assembled below the existing AttemptEvaluator seam."""

    tasks: Mapping[str, ShoppingTaskRef]
    port: InMemoryShopSimulatorAdapter
    baseline_evaluator: ShopSimulatorAttemptEvaluator
    skill_evaluator: ShopSimulatorAttemptEvaluator


_FixedOutcomeKind = Literal[
    "fail_to_pass",
    "pass_to_fail",
    "both_pass",
    "both_fail",
]
_OUTCOME_ROTATION: tuple[_FixedOutcomeKind, ...] = (
    "fail_to_pass",
    "pass_to_fail",
    "both_pass",
    "both_fail",
)


@dataclass(frozen=True, slots=True)
class _FixedDevelopCase:
    task: ShoppingTaskRef
    outcome: _FixedOutcomeKind
    public_cue: str


def fixed_public_source_groups() -> tuple[ShoppingSourceGroup, ...]:
    """Return only course-original group identities visible to learners."""

    counts = (("creator", 2), ("develop", 3))
    return tuple(
        ShoppingSourceGroup(
            source_group_id=f"fixed-{split}-group-{index:02d}",
            semantic_family_id=f"fixed-{split}-family-{index:02d}",
            split=split,  # type: ignore[arg-type]
        )
        for split, count in counts
        for index in range(1, count + 1)
    )


def fixed_develop_tasks(
    profile: LoadedShoppingProfile,
) -> dict[str, ShoppingTaskRef]:
    slots = expand_public_source_groups(profile.profile, fixed_public_source_groups())
    develop = tuple(task for task in public_task_refs(slots) if task.split == "develop")
    return {
        f"shopping-develop-{index:02d}": task for index, task in enumerate(develop, 1)
    }


def _fixed_develop_cases(
    profile: LoadedShoppingProfile,
    tasks: Mapping[str, ShoppingTaskRef],
) -> dict[str, _FixedDevelopCase]:
    slots = tuple(
        slot
        for slot in expand_public_source_groups(
            profile.profile, fixed_public_source_groups()
        )
        if slot.split == "develop"
    )
    group_order = {
        group_id: index
        for index, group_id in enumerate(
            sorted({slot.source_group_id for slot in slots})
        )
    }
    scenario_order = {
        scenario: index for index, scenario in enumerate(profile.profile.scenarios)
    }
    cases: dict[str, _FixedDevelopCase] = {}
    for (case_id, task), slot in zip(tasks.items(), slots, strict=True):
        if task.opaque_slot != slot.episode_slot or task.scenario is not slot.scenario:
            raise ValueError(
                "fixed develop task projection drifted from its source group"
            )
        outcome = _OUTCOME_ROTATION[
            (group_order[slot.source_group_id] + scenario_order[slot.scenario])
            % len(_OUTCOME_ROTATION)
        ]
        if outcome == "fail_to_pass":
            cue = FIXED_CUE_CONSTRAINT_SEARCH
        elif outcome == "pass_to_fail" and slot.scenario.value == "single_persona":
            cue = FIXED_CUE_FAREWELL
        elif outcome == "pass_to_fail":
            cue = FIXED_CUE_PREFERENCE_EXPLORE
        elif outcome == "both_pass":
            cue = FIXED_CUE_STANDARD_SEARCH
        else:
            cue = FIXED_CUE_NO_ELIGIBLE
        cases[case_id] = _FixedDevelopCase(
            task=task,
            outcome=outcome,
            public_cue=cue,
        )
    return cases


def _reward(success: bool) -> RawShopSimulatorReward:
    value = Decimal(1 if success else 0)
    return RawShopSimulatorReward(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="raw_shop_simulator_reward",
        reward=value,
        r_type=value,
        r_att=value,
        r_option=value,
        r_price=value,
        source_names=("reward", "reward_detail"),
    )


def _terminal(nonce: str, *, success: bool) -> EpisodeStep:
    return EpisodeStep(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="shopping_episode_step",
        episode_nonce=nonce,
        sequence=1,
        observation=ShoppingObservation(text="本轮已结束"),
        terminal=True,
        terminal_reason="upstream_terminal",
        raw_reward=_reward(success),
    )


def _fixed_adapter(
    cases: Mapping[str, _FixedDevelopCase],
) -> InMemoryShopSimulatorAdapter:
    fixtures: dict[str, InMemoryEpisodeFixture] = {}
    for case in cases.values():
        task = case.task
        nonce = f"fixed-fixture-{task.opaque_slot}"
        baseline_success = case.outcome in {"pass_to_fail", "both_pass"}
        constrained_success = case.outcome in {"fail_to_pass", "both_pass"}
        purchase_offer = ShoppingPurchaseOffer(
            product_id=f"fixed-product-{task.opaque_slot[-8:]}",
            option="默认规格",
            quantity=1,
            price_amount_minor=10000,
            price_currency="CNY",
            detail_viewed=True,
            eligible=True,
        )
        transitions = [
            InMemoryActionTransition(
                expected=ShoppingAction.search(FIXED_BROAD_QUERY),
                step=_terminal(nonce, success=baseline_success),
            ),
            InMemoryActionTransition(
                expected=ShoppingAction.search(FIXED_CONSTRAINT_QUERY),
                step=_terminal(nonce, success=constrained_success),
            ),
        ]
        farewell = case.public_cue == FIXED_CUE_FAREWELL
        if farewell:
            transitions.append(
                InMemoryActionTransition(
                    expected=ShoppingAction.purchase("确认购买"),
                    step=_terminal(nonce, success=True),
                )
            )
        fixtures[task.opaque_slot] = InMemoryEpisodeFixture(
            start=EpisodeStart(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="shopping_episode_start",
                episode_nonce=nonce,
                task_slot=task.opaque_slot,
                scenario=task.scenario,
                sequence=0,
                observation=ShoppingObservation(
                    text=case.public_cue,
                    allows_search=True,
                    shopper_state="farewell" if farewell else "active",
                    available_actions=(
                        (
                            ShoppingAvailableAction(
                                label="确认购买",
                                kind="purchase",
                                purchase_offer=purchase_offer,
                            ),
                        )
                        if farewell
                        else ()
                    ),
                ),
                terminal=False,
                initial_authorization=False,
            ),
            steps=(),
            transitions=tuple(transitions),
        )
    return InMemoryShopSimulatorAdapter(fixtures)


def _private_expectations(
    cases: Mapping[str, _FixedDevelopCase],
) -> dict[str, ShoppingPrivateExpectation]:
    return {
        case_id: ShoppingPrivateExpectation(
            product_id=f"fixed-product-{case.task.opaque_slot[-8:]}",
            option="默认规格",
            quantity=1,
            max_price_amount_minor=10000,
            price_currency="CNY",
        )
        for case_id, case in cases.items()
    }


def build_fixed_develop_evaluation(
    profile: LoadedShoppingProfile,
    *,
    learner_skill_sha256: str,
    learner_skill_source: Path,
) -> FixedDevelopEvaluation:
    """Build two fresh evaluator sides with all four visible pair categories."""

    tasks = fixed_develop_tasks(profile)
    cases = _fixed_develop_cases(profile, tasks)
    port = _fixed_adapter(cases)
    expectations = _private_expectations(cases)
    baseline = ShopSimulatorAttemptEvaluator(
        port=port,
        tasks=tasks,
        engine_factory=lambda _context: ScriptedShoppingEngine(
            (ScriptedShoppingTurn(ShoppingActionKind.SEARCH, "宽泛查询"),)
        ),
        profile_sha256=profile.profile_sha256,
        measurement_level=MeasurementLevel.SYNTHETIC_OFFLINE,
        model_lock_sha256=profile.profile.agent_model_sha256,
        skill_sha256=_EMPTY_SHA256,
        protocol_sha256=profile.profile.turn_policy_sha256,
        private_expectations=expectations,
    )
    skill_policy = FixedShoppingSkillPolicy.from_skill_source(learner_skill_source)

    skill = ShopSimulatorAttemptEvaluator(
        port=port,
        tasks=tasks,
        engine_factory=lambda _context: FixedShoppingPolicyEngine(skill_policy),
        profile_sha256=profile.profile_sha256,
        measurement_level=MeasurementLevel.SYNTHETIC_OFFLINE,
        model_lock_sha256=profile.profile.agent_model_sha256,
        skill_sha256=learner_skill_sha256,
        skill_source=learner_skill_source,
        protocol_sha256=profile.profile.turn_policy_sha256,
        private_expectations=expectations,
    )
    return FixedDevelopEvaluation(
        tasks=tasks,
        port=port,
        baseline_evaluator=baseline,
        skill_evaluator=skill,
    )


__all__ = [
    "FixedDevelopEvaluation",
    "build_fixed_develop_evaluation",
    "fixed_develop_tasks",
    "fixed_public_source_groups",
]
