"""Gate milestone: shopping metrics, guardrails, and Registry branch."""

from __future__ import annotations

from collections.abc import Callable, Mapping


def project_gate_metrics(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Gate: project shopping Gate metrics")


def apply_guardrails(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Gate: apply safety and quality guardrails")


def select_registry_branch(probe: object) -> str:
    del probe
    raise NotImplementedError("Capstone Gate: choose the authorized Registry branch")


def execute_target(
    command_id: str,
    probe: Mapping[str, object],
    validate_policy: Callable[[Mapping[str, object]], str],
    execute_once: Callable[[], int],
) -> int:
    del command_id, probe, validate_policy, execute_once
    raise NotImplementedError(
        "Capstone Gate: validate learner policy before target execution"
    )


def gate_policy(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Gate: lock shopping thresholds and guardrails")


def candidate_gate(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Gate: run the shared eight-stage Gate")


def registry_branch(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError(
        "Capstone Gate: apply only the authorized Registry branch"
    )
