"""Automation solution: validate lifecycle policy and use shared release paths."""

from collections.abc import Callable, Mapping

from ses.automation.capstone import build_capstone_index as build_completion_index
from ses.contracts import FinalLifecycle, GateOutcome
from ses.shopping.automation import build_shopping_capstone_orchestrator as build_loop
from ses.skills.release import install_current_accepted as install_accepted
from ses.skills.release import package_current_accepted as package_accepted


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} probe must be an object")
    return value


def plan_loop(value: object) -> Mapping[str, object]:
    if not isinstance(value, list):
        raise TypeError("automation rounds must be a list")
    outcomes = []
    for item in value:
        row = _mapping(item, "automation round")
        outcome = row.get("outcome")
        if outcome not in {GateOutcome.ACCEPTED.value, GateOutcome.REJECTED.value}:
            raise ValueError("automation round has an unknown outcome")
        outcomes.append(outcome)
    accepted = outcomes.count(GateOutcome.ACCEPTED.value)
    rejected = outcomes.count(GateOutcome.REJECTED.value)
    return {
        "round_count": len(outcomes),
        "accepted_count": accepted,
        "rejected_count": rejected,
        "bounded": len(outcomes) >= 2 and accepted >= 1 and rejected >= 1,
    }


def final_eligibility(value: object) -> Mapping[str, object]:
    probe = _mapping(value, "final")
    call_count = probe.get("call_count")
    safety = probe.get("safety_violation_count")
    if type(call_count) is not int or type(safety) is not int:
        raise TypeError("final counts must be integers")
    one_time = call_count == 1
    return {
        "eligible": probe.get("lifecycle") == FinalLifecycle.INDEPENDENT_CAPSTONE.value
        and one_time
        and safety == 0,
        "one_time": one_time,
    }


def package_eligibility(value: object) -> Mapping[str, object]:
    probe = _mapping(value, "package")
    accepted_only = isinstance(
        probe.get("registry_current_accepted"), str
    ) and probe.get("registry_current_accepted") == probe.get("final_subject")
    eligible = (
        accepted_only
        and probe.get("final_completed") is True
        and probe.get("final_safety_violation_count") == 0
    )
    return {"eligible": eligible, "accepted_only": accepted_only}


def execute_target(
    command_id: str,
    probe: Mapping[str, object],
    validate_policy: Callable[[Mapping[str, object]], str],
    execute_once: Callable[[], int],
) -> int:
    del command_id
    validate_policy(
        {
            "loop_decision": plan_loop(probe.get("rounds")),
            "final_decision": final_eligibility(probe.get("final")),
            "package_decision": package_eligibility(probe.get("package")),
        }
    )
    return execute_once()


__all__ = [
    "build_completion_index",
    "build_loop",
    "execute_target",
    "final_eligibility",
    "install_accepted",
    "package_accepted",
    "package_eligibility",
    "plan_loop",
]
