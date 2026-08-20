"""Evolve milestone: shopping diagnosis and bounded Patch decisions."""

from __future__ import annotations

from collections.abc import Callable, Mapping


def diagnose_failure(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Evolve: attribute the reviewed failure")


def propose_patch(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Evolve: decide bounded Patch eligibility")


def execute_target(
    command_id: str,
    probe: Mapping[str, object],
    validate_policy: Callable[[Mapping[str, object]], str],
    execute_once: Callable[[], int],
) -> int:
    del command_id, probe, validate_policy, execute_once
    raise NotImplementedError(
        "Capstone Evolve: validate learner policy before target execution"
    )


def diagnosis_policy(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Evolve: classify shopping failure evidence")


def updater_policy(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Evolve: propose an evidence-linked Patch")


def evolution_stage(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Evolve: publish the bounded candidate")
