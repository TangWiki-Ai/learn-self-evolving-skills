"""Eval solution: validate grade/pair policy and use the shared workflow."""

from collections.abc import Callable, Mapping
from decimal import Decimal

from ses.contracts import SchemaVersion
from ses.contracts.shopping import RawShopSimulatorReward
from ses.shopping.course_workflow import run_shopping_paired_stage as paired_stage
from ses.shopping.grading import (
    LockedShoppingGradePolicy,
    ShoppingGradePolicy,
    project_shopping_metrics,
)


def grade_policy() -> ShoppingGradePolicy:
    return LockedShoppingGradePolicy()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} probe must be an object")
    return value


def project_grade(value: object) -> Mapping[str, object]:
    probe = _mapping(value, "reward")
    safety = probe.get("safety_violation_count")
    if type(safety) is not int:
        raise TypeError("reward safety count must be an integer")

    def reward_decimal(name: str) -> Decimal:
        raw_value = probe.get(name)
        if not isinstance(raw_value, str):
            raise TypeError(f"reward {name} must be a decimal string")
        return Decimal(raw_value)

    raw = RawShopSimulatorReward(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="raw_shop_simulator_reward",
        reward=reward_decimal("reward"),
        reward_detail_present=True,
        r_type=reward_decimal("r_type"),
        r_att=reward_decimal("r_att"),
        r_option=reward_decimal("r_option"),
        r_price=reward_decimal("r_price"),
        source_names=("course_original_policy_probe",),
    )
    metric = project_shopping_metrics(
        raw=raw,
        raw_reward_ref=None,
        purchased_asin="opaque-offer",
        private_goal_asin="opaque-offer",
        safety_violation_count=safety,
    )
    return {
        "full_success": metric.benchmark_success,
        "strict_reward": str(metric.r_strict),
        "safety_violation_count": metric.safety_violation_count,
        "course_pass": metric.course_pass,
    }


def compare_pair(value: object) -> Mapping[str, object]:
    probe = _mapping(value, "pair")
    comparable = all(
        probe.get(name) is True
        for name in (
            "same_profile",
            "same_model",
            "same_protocol",
            "fresh_sessions",
        )
    )
    baseline_pass = probe.get("baseline_pass") is True
    candidate_pass = probe.get("candidate_pass") is True
    flip = (
        "improved"
        if not baseline_pass and candidate_pass
        else "regressed"
        if baseline_pass and not candidate_pass
        else "unchanged"
    )
    return {"comparable": comparable, "flip": flip}


def execute_target(
    command_id: str,
    probe: Mapping[str, object],
    validate_policy: Callable[[Mapping[str, object]], str],
    execute_once: Callable[[], int],
) -> int:
    del command_id
    validate_policy(
        {
            "grade_projection": project_grade(probe.get("reward")),
            "pair_comparison": compare_pair(probe.get("pair")),
        }
    )
    return execute_once()


__all__ = [
    "compare_pair",
    "execute_target",
    "grade_policy",
    "paired_stage",
    "project_grade",
]
