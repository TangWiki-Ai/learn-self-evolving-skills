"""Student-owned seams for benchmark candidate mining."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ses.testset.cluster import (
    ClusterAdapter,
    ClusterAssignment,
    LabelComparison,
)
from ses.testset.difficulty import TauDifficulty
from ses.testset.scrub import ScrubbedConversation, ScrubResult


def scrub_product_defect(
    conversations: Sequence[Mapping[str, object]],
) -> ScrubResult:
    """Select the exact ABCD slice, then preserve aligned original/delexed pairs."""

    raise NotImplementedError("Lesson 5: implement exact slicing and scrubbing")


def cluster_and_compare_labels(
    records: Sequence[ScrubbedConversation],
    adapter: ClusterAdapter,
) -> tuple[
    tuple[ClusterAssignment, ...],
    tuple[LabelComparison, LabelComparison],
]:
    """Cluster scrubbed text and compare assignments with flow/subflow labels."""

    raise NotImplementedError("Lesson 5: implement clustering and label comparison")


def aggregate_tau2_by_task(
    tasks: Sequence[Mapping[str, object]],
    result_documents: Mapping[str, object],
) -> tuple[TauDifficulty, ...]:
    """Collapse repeated tau2 runs to one auditable difficulty record per task."""

    raise NotImplementedError("Lesson 5: implement task-level tau2 aggregation")
