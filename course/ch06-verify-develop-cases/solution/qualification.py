"""Lesson 6 solution delegates every decision to the production protocol."""

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

from ses.testset.curation import (
    CurationBundle,
    FixedCurationModel,
    curate_sources,
)
from ses.testset.verified import VerifiedCase, qualify_cases
from ses.testset.verified import assert_split_safe as protect_split
from ses.testset.verified import generate_controlled_variant as verify_variant


def curate_candidate_sources(
    source_ids: Sequence[str], source_path: Path, response_fixture: Path
) -> CurationBundle:
    """Exercise the production fixed/live-neutral curation protocol offline."""

    return asyncio.run(
        curate_sources(
            source_ids=source_ids,
            source_path=source_path,
            model=FixedCurationModel.from_path(response_fixture),
        )
    )


def calibrate_case(case: VerifiedCase) -> Mapping[str, object]:
    """Read statuses produced by the production calibration protocol."""
    return case.calibration


__all__ = [
    "calibrate_case",
    "curate_candidate_sources",
    "protect_split",
    "qualify_cases",
    "verify_variant",
]
