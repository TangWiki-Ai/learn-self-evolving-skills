"""Create solution: validate learner policy and use the shared workflows."""

from collections.abc import Callable, Mapping
from decimal import Decimal

from ses.shopping.course_workflow import (
    SHOPPING_STATIC_GATE_POLICY,
    SHOPPING_TRIGGER_PROMPTS,
)
from ses.shopping.course_workflow import run_shopping_create_stage as create_stage
from ses.shopping.course_workflow import run_shopping_static_stage as static_stage
from ses.shopping.course_workflow import run_shopping_trigger_stage as trigger_stage


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} probe must be an object")
    return value


def project_seed(value: object) -> Mapping[str, object]:
    probe = _mapping(value, "seed")
    behaviors = probe.get("reusable_behaviors")
    if not isinstance(behaviors, list) or not all(
        isinstance(item, str) and item for item in behaviors
    ):
        raise TypeError("seed behaviors must be nonempty strings")
    accepted = len(behaviors) >= 2 and probe.get("contains_product_identity") is False
    return {"accepted": accepted, "behavior_count": len(behaviors)}


def static_decision(value: object) -> Mapping[str, object]:
    probe = _mapping(value, "Static")
    tools = probe.get("tools")
    if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
        raise TypeError("Static tools must be strings")
    purchase_separate = probe.get("purchase_is_separate") is True
    accepted = (
        set(tools) == set(SHOPPING_STATIC_GATE_POLICY.supported_tools)
        and probe.get("contains_forbidden_identifier") is False
        and purchase_separate
    )
    return {"accepted": accepted, "purchase_separate": purchase_separate}


def trigger_decision(value: object) -> Mapping[str, object]:
    if not isinstance(value, list):
        raise TypeError("Trigger probe must be a list")
    expected = {row.prompt_id: row.expected_trigger for row in SHOPPING_TRIGGER_PROMPTS}
    true_positive = false_positive = false_negative = 0
    for item in value:
        row = _mapping(item, "Trigger row")
        prompt_id = row.get("prompt_id")
        triggered = row.get("triggered")
        if not isinstance(prompt_id, str) or type(triggered) is not bool:
            raise TypeError("Trigger rows require prompt_id and bool triggered")
        expected_trigger = expected.get(prompt_id)
        if expected_trigger is None:
            raise ValueError("Trigger probe is not in the locked suite")
        true_positive += int(expected_trigger and triggered)
        false_positive += int(not expected_trigger and triggered)
        false_negative += int(expected_trigger and not triggered)
    precision = Decimal(true_positive) / Decimal(true_positive + false_positive)
    recall = Decimal(true_positive) / Decimal(true_positive + false_negative)
    return {"precision": str(precision), "recall": str(recall)}


def execute_target(
    command_id: str,
    probe: Mapping[str, object],
    validate_policy: Callable[[Mapping[str, object]], str],
    execute_once: Callable[[], int],
) -> int:
    del command_id
    validate_policy(
        {
            "seed_projection": project_seed(probe.get("seed")),
            "static_decision": static_decision(probe.get("static")),
            "trigger_decision": trigger_decision(probe.get("trigger")),
        }
    )
    return execute_once()


__all__ = [
    "create_stage",
    "execute_target",
    "project_seed",
    "static_decision",
    "static_stage",
    "trigger_decision",
    "trigger_stage",
]
