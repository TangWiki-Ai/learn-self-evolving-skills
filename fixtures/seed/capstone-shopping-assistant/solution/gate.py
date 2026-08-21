"""Gate solution: validate metric/guardrail policy and use shared truths."""

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import cast

from ses.contracts import GateOutcome, GateReason
from ses.evolution.gate import run_candidate_gate as candidate_gate
from ses.evolution.governance import govern_candidate as registry_branch
from ses.shopping.gate import shopping_gate_policy as gate_policy


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} probe must be an object")
    return value


def project_gate_metrics(value: object) -> Mapping[str, object]:
    probe = _mapping(value, "Gate metrics")
    accepted_full = probe.get("accepted_full_success_count")
    candidate_full = probe.get("candidate_full_success_count")
    safety = probe.get("candidate_safety_violation_count")
    if (
        type(accepted_full) is not int
        or type(candidate_full) is not int
        or type(safety) is not int
    ):
        raise TypeError("Gate counts must be integers")
    accepted_strict = Decimal(str(probe.get("accepted_mean_strict_reward")))
    candidate_strict = Decimal(str(probe.get("candidate_mean_strict_reward")))
    return {
        "full_success_delta": candidate_full - accepted_full,
        "strict_delta": str(candidate_strict - accepted_strict),
        "safety_violation_count": safety,
    }


def apply_guardrails(value: object) -> Mapping[str, object]:
    probe = _mapping(value, "Gate guardrail")
    accepted_full = probe.get("accepted_full_success_count")
    candidate_full = probe.get("candidate_full_success_count")
    safety = probe.get("candidate_safety_violation_count")
    critical = probe.get("critical_regression_count")
    if any(
        type(item) is not int
        for item in (accepted_full, candidate_full, safety, critical)
    ):
        raise TypeError("Gate guardrail counts must be integers")
    accepted_full = cast(int, accepted_full)
    candidate_full = cast(int, candidate_full)
    safety = cast(int, safety)
    critical = cast(int, critical)
    if safety > 0:
        outcome, reason = GateOutcome.REJECTED, GateReason.SAFETY_VIOLATION
    elif critical > 0:
        outcome, reason = GateOutcome.REJECTED, GateReason.CRITICAL_REGRESSION
    elif candidate_full <= accepted_full:
        outcome = GateOutcome.REJECTED
        reason = (
            GateReason.TIE
            if candidate_full == accepted_full
            else GateReason.OVERALL_REGRESSION
        )
    elif Decimal(str(probe.get("candidate_mean_strict_reward"))) < Decimal(
        str(probe.get("accepted_mean_strict_reward"))
    ):
        outcome, reason = GateOutcome.REJECTED, GateReason.STRICT_REGRESSION
    else:
        outcome, reason = GateOutcome.ACCEPTED, GateReason.ACCEPTED
    return {"outcome": outcome.value, "reason": reason.value}


def select_registry_branch(value: object) -> str:
    decision = _mapping(value, "Gate decision")
    return (
        "promote" if decision.get("outcome") == GateOutcome.ACCEPTED.value else "retain"
    )


def execute_target(
    command_id: str,
    probe: Mapping[str, object],
    validate_policy: Callable[[Mapping[str, object]], str],
    execute_once: Callable[[], int],
) -> int:
    del command_id
    metrics = _mapping(probe.get("metrics"), "Gate metrics")
    decision = apply_guardrails(metrics)
    validate_policy(
        {
            "metric_projection": project_gate_metrics(metrics),
            "guardrail_decision": decision,
            "registry_branch": select_registry_branch(decision),
        }
    )
    return execute_once()


__all__ = [
    "apply_guardrails",
    "candidate_gate",
    "execute_target",
    "gate_policy",
    "project_gate_metrics",
    "registry_branch",
    "select_registry_branch",
]
