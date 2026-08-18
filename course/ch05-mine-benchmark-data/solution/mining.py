"""Reference connections to the production benchmark-mining modules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ses.testset.cluster import (
    ClusterAdapter,
    ClusterAssignment,
    ClusterItem,
    LabelComparison,
    assign_clusters,
    compare_cluster_labels,
)
from ses.testset.difficulty import TauDifficulty, aggregate_tau_difficulty
from ses.testset.scrub import ScrubbedConversation, ScrubResult, scrub_abcd
from ses.testset.sources import filter_abcd_product_defect


def scrub_product_defect(
    conversations: Sequence[Mapping[str, object]],
) -> ScrubResult:
    """Select the exact ABCD slice, then preserve aligned original/delexed pairs."""

    return scrub_abcd(filter_abcd_product_defect(conversations))


def cluster_and_compare_labels(
    records: Sequence[ScrubbedConversation],
    adapter: ClusterAdapter,
) -> tuple[
    tuple[ClusterAssignment, ...],
    tuple[LabelComparison, LabelComparison],
]:
    """Cluster scrubbed text and compare assignments with flow/subflow labels."""

    items = tuple(
        ClusterItem(
            item_id=record.source_id,
            text=record.normalized_text,
            source_kind="abcd_roleplay_benchmark",
        )
        for record in records
    )
    assignments = assign_clusters(items, adapter)
    flow = compare_cluster_labels(
        assignments,
        {record.source_id: record.flow for record in records},
        label_name="flow",
    )
    subflow = compare_cluster_labels(
        assignments,
        {record.source_id: record.subflow for record in records},
        label_name="subflow",
    )
    return assignments, (flow, subflow)


def aggregate_tau2_by_task(
    tasks: Sequence[Mapping[str, object]],
    result_documents: Mapping[str, object],
) -> tuple[TauDifficulty, ...]:
    """Collapse repeated tau2 runs to one auditable difficulty record per task."""

    return aggregate_tau_difficulty(tasks, result_documents)
