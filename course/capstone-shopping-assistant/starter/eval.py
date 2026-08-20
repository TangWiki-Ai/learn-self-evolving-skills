"""Eval milestone: reward projection, grading, and fresh-pair decisions."""

from __future__ import annotations

from collections.abc import Callable, Mapping


def project_grade(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Eval: project raw reward into CaseGrade inputs")


def compare_pair(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Eval: decide fresh-pair comparability")


def execute_target(
    command_id: str,
    probe: Mapping[str, object],
    validate_policy: Callable[[Mapping[str, object]], str],
    execute_once: Callable[[], int],
) -> int:
    del command_id, probe, validate_policy, execute_once
    raise NotImplementedError(
        "Capstone Eval: validate learner policy before target execution"
    )


def grade_policy(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Eval: project raw reward and safety evidence")


def paired_stage(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Eval: validate and aggregate a fresh pair")
