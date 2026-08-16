"""Lesson 6 solution delegates every decision to the production protocol."""

from collections.abc import Mapping

from ses.testset.verified import (
    VerifiedCase,
    qualify_cases,
)
from ses.testset.verified import (
    assert_split_safe as protect_split,
)
from ses.testset.verified import (
    generate_controlled_variant as verify_variant,
)


def calibrate_case(case: VerifiedCase) -> Mapping[str, object]:
    """Read statuses produced by the production calibration protocol."""
    return case.calibration


__all__ = ["calibrate_case", "protect_split", "qualify_cases", "verify_variant"]
