"""Evolve solution: validate diagnosis/Patch policy and use shared workflows."""

from collections.abc import Callable, Mapping

from ses.evolution.diagnosis import (
    SHOPPING_DIAGNOSIS_POLICY,
    FailureDiagnosisPolicy,
    FailureObservation,
    attribute_failure,
)
from ses.evolution.diagnosis import analyze_failure_evidence as diagnosis_stage
from ses.evolution.patches import PatchValidationError, validate_target
from ses.evolution.updater import SHOPPING_UPDATER_POLICY, UpdaterPolicy
from ses.evolution.workflow import run_evolution_workflow as evolution_stage


def diagnosis_policy() -> FailureDiagnosisPolicy:
    return SHOPPING_DIAGNOSIS_POLICY


def updater_policy() -> UpdaterPolicy:
    return SHOPPING_UPDATER_POLICY


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} probe must be an object")
    return value


def diagnose_failure(value: object) -> Mapping[str, object]:
    probe = _mapping(value, "failure")
    names = (
        "runtime_healthy",
        "case_gold_healthy",
        "judge_simulator_healthy",
        "skill_failed",
    )
    if any(type(probe.get(name)) is not bool for name in names):
        raise TypeError("failure probe flags must be booleans")
    diagnosis = attribute_failure(
        FailureObservation(
            runtime_healthy=probe["runtime_healthy"] is True,
            case_gold_healthy=probe["case_gold_healthy"] is True,
            judge_simulator_healthy=probe["judge_simulator_healthy"] is True,
            skill_failed=probe["skill_failed"] is True,
        )
    )
    return {
        "attribution": diagnosis.attribution.value,
        "patch_allowed": diagnosis.patch_allowed
        and SHOPPING_DIAGNOSIS_POLICY.require_shopping_evidence,
    }


def propose_patch(value: object) -> Mapping[str, object]:
    probe = _mapping(value, "Patch")
    operations = probe.get("operations")
    if not isinstance(operations, list):
        raise TypeError("Patch operations must be a list")
    eligible = 1 <= len(operations) <= SHOPPING_UPDATER_POLICY.max_operations
    for item in operations:
        operation = _mapping(item, "Patch operation")
        target = operation.get("target")
        card_ids = operation.get("failure_card_ids")
        if (
            not isinstance(target, str)
            or not isinstance(card_ids, list)
            or not card_ids
        ):
            eligible = False
            continue
        try:
            validate_target(target)
        except PatchValidationError:
            eligible = False
    return {"eligible": eligible, "operation_count": len(operations)}


def execute_target(
    command_id: str,
    probe: Mapping[str, object],
    validate_policy: Callable[[Mapping[str, object]], str],
    execute_once: Callable[[], int],
) -> int:
    del command_id
    validate_policy(
        {
            "diagnosis": diagnose_failure(probe.get("failure")),
            "patch_decision": propose_patch(probe.get("patch")),
        }
    )
    return execute_once()


__all__ = [
    "diagnose_failure",
    "diagnosis_policy",
    "diagnosis_stage",
    "evolution_stage",
    "execute_target",
    "propose_patch",
    "updater_policy",
]
