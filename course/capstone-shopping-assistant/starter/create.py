"""Create milestone: projection, Static, and Trigger decisions."""

from __future__ import annotations

from collections.abc import Callable, Mapping


def project_seed(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Create: project reusable seed behavior")


def static_decision(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Create: decide the shopping Static policy")


def trigger_decision(probe: object) -> Mapping[str, object]:
    del probe
    raise NotImplementedError("Capstone Create: decide the 10/10 Trigger policy")


def execute_target(
    command_id: str,
    probe: Mapping[str, object],
    validate_policy: Callable[[Mapping[str, object]], str],
    execute_once: Callable[[], int],
) -> int:
    del command_id, probe, validate_policy, execute_once
    raise NotImplementedError(
        "Capstone Create: validate learner policy before target execution"
    )


def create_stage(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Create: project seeds and create learner v0")


def static_stage(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Create: implement the shopping Static policy")


def trigger_stage(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Capstone Create: implement the 10/10 Trigger suite")
