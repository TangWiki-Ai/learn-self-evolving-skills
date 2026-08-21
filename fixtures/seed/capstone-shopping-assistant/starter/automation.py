"""Automation milestone: loop, one-time final, and accepted-only release."""

from __future__ import annotations

from collections.abc import Callable, Mapping


def plan_loop(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Automation: decide bounded loop completion")


def final_eligibility(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Automation: enforce one-time final eligibility")


def package_eligibility(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError(
        "Capstone Automation: enforce accepted-only package eligibility"
    )


def execute_target(
    command_id: str,
    probe: Mapping[str, object],
    validate_policy: Callable[[Mapping[str, object]], str],
    execute_once: Callable[[], int],
) -> int:
    del command_id, probe, validate_policy, execute_once
    raise NotImplementedError(
        "Capstone Automation: validate learner policy before target execution"
    )


def build_loop(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError(
        "Capstone Automation: assemble the bounded two-round loop"
    )


def build_completion_index(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError(
        "Capstone Automation: replay learner evidence and build CapstoneIndex"
    )


def package_accepted(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Automation: enforce accepted-only packaging")


def install_accepted(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError(
        "Capstone Automation: install the verified release package"
    )
