from __future__ import annotations

import pytest

from ses.testset.cluster import (
    REPRESENTATIVE_SELECTION_METHOD,
    ClusterAssignment,
    ClusterContractError,
    ClusterItem,
    SklearnTfidfClusterAdapter,
    assign_clusters,
    compare_cluster_labels,
    summarize_clusters,
)


class DeterministicFakeClusterAdapter:
    def __init__(self, labels: dict[str, str]) -> None:
        self._labels = labels

    @property
    def adapter_id(self) -> str:
        return "deterministic-fake-cluster:v1"

    def cluster(self, items: tuple[ClusterItem, ...]) -> tuple[ClusterAssignment, ...]:
        # Reverse output order to prove consumers join by stable item ID.
        return tuple(
            ClusterAssignment(
                item_id=item.item_id,
                cluster_id=self._labels[item.item_id],
                confidence=1.0,
            )
            for item in reversed(items)
            if item.item_id in self._labels
        )


class NearEqualConfidenceAdapter:
    def __init__(self, confidence: float) -> None:
        self._confidence = confidence

    @property
    def adapter_id(self) -> str:
        return "near-equal-confidence:v1"

    def cluster(self, items: tuple[ClusterItem, ...]) -> tuple[ClusterAssignment, ...]:
        return tuple(
            ClusterAssignment(item.item_id, "one", self._confidence) for item in items
        )


def test_cluster_adapter_output_is_joined_by_id_and_compared_to_labels() -> None:
    items = (
        ClusterItem("abcd:1", "wrong size", "abcd"),
        ClusterItem("abcd:2", "size does not fit", "abcd"),
        ClusterItem("abcd:3", "where is my refund", "abcd"),
        ClusterItem("abcd:4", "refund has not arrived", "abcd"),
    )
    assignments = assign_clusters(
        items,
        DeterministicFakeClusterAdapter(
            {
                "abcd:1": "cluster-size",
                "abcd:2": "cluster-size",
                "abcd:3": "cluster-refund",
                "abcd:4": "cluster-refund",
            }
        ),
    )

    assert [assignment.item_id for assignment in assignments] == [
        item.item_id for item in items
    ]
    subflow = compare_cluster_labels(
        assignments,
        {
            "abcd:1": "return_size",
            "abcd:2": "return_size",
            "abcd:3": "refund_status",
            "abcd:4": "refund_status",
        },
        label_name="subflow",
    )
    flow = compare_cluster_labels(
        assignments,
        {item.item_id: "product_defect" for item in items},
        label_name="flow",
    )

    assert subflow.adjusted_rand_index == pytest.approx(1.0)
    assert subflow.normalized_mutual_info == pytest.approx(1.0)
    assert subflow.homogeneity == pytest.approx(1.0)
    assert subflow.completeness == pytest.approx(1.0)
    assert subflow.v_measure == pytest.approx(1.0)
    assert flow.informative is False
    assert flow.reason == "single_reference_label"
    assert flow.true_label_count == 1
    assert flow.cluster_count == 2


def test_cluster_adapter_must_return_one_assignment_per_item() -> None:
    items = (ClusterItem("abcd:1", "return it", "abcd"),)

    with pytest.raises(ClusterContractError, match="missing"):
        assign_clusters(items, DeterministicFakeClusterAdapter({}))


def test_cluster_adapter_identity_captures_reproducibility_parameters() -> None:
    first = SklearnTfidfClusterAdapter(n_clusters=6, random_state=7)
    second = SklearnTfidfClusterAdapter(n_clusters=12, random_state=7)

    assert first.adapter_id != second.adapter_id
    assert "n_clusters=6" in first.adapter_id
    assert "random_state=7" in first.adapter_id


def test_cluster_confidence_is_quantized_at_the_canonical_adapter_boundary() -> None:
    items = (ClusterItem("abcd:1", "return it", "abcd"),)

    lower = assign_clusters(items, NearEqualConfidenceAdapter(0.12345678901234))
    upper = assign_clusters(items, NearEqualConfidenceAdapter(0.12345678901235))

    assert lower == upper
    assert lower[0].confidence == 0.123456789012
    assert "confidence_quantization=12dp" in SklearnTfidfClusterAdapter().adapter_id


def test_cluster_representatives_are_auditable_and_input_order_independent() -> None:
    items = (
        ClusterItem("abcd:1", "wrong size", "abcd_roleplay_benchmark"),
        ClusterItem("abcd:2", "size does not fit", "abcd_roleplay_benchmark"),
        ClusterItem("abcd:3", "refund has not arrived", "abcd_roleplay_benchmark"),
        ClusterItem("abcd:4", "still waiting for refund", "abcd_roleplay_benchmark"),
        ClusterItem("abcd:5", "refund is pending", "abcd_roleplay_benchmark"),
    )
    assignments = (
        ClusterAssignment("abcd:1", "cluster:size", 0.8),
        ClusterAssignment("abcd:2", "cluster:size", 0.9),
        ClusterAssignment("abcd:3", "cluster:refund", None),
        ClusterAssignment("abcd:4", "cluster:refund", 0.7),
        ClusterAssignment("abcd:5", "cluster:refund", 0.7),
    )

    forward = summarize_clusters(items, assignments, representatives_per_cluster=2)
    reversed_input = summarize_clusters(
        tuple(reversed(items)),
        tuple(reversed(assignments)),
        representatives_per_cluster=2,
    )

    assert forward == reversed_input
    assert [summary.cluster_id for summary in forward] == [
        "cluster:refund",
        "cluster:size",
    ]
    refund, size = forward
    assert refund.member_count == 3
    assert refund.representative_selection_method == REPRESENTATIVE_SELECTION_METHOD
    assert [sample.item_id for sample in refund.representative_samples] == [
        "abcd:4",
        "abcd:5",
    ]
    assert refund.representative_samples[0].text == "still waiting for refund"
    assert refund.representative_samples[0].source_kind == "abcd_roleplay_benchmark"
    assert refund.representative_samples[0].selection_reason == (
        "rank=1;method=" + REPRESENTATIVE_SELECTION_METHOD
    )
    assert size.member_count == 2
    assert [sample.item_id for sample in size.representative_samples] == [
        "abcd:2",
        "abcd:1",
    ]


def test_cluster_representative_summary_validates_complete_assignments() -> None:
    items = (ClusterItem("abcd:1", "return it", "abcd_roleplay_benchmark"),)

    with pytest.raises(ClusterContractError, match="missing assignments"):
        summarize_clusters(items, ())

    with pytest.raises(ValueError, match="must be positive"):
        summarize_clusters(
            items,
            (ClusterAssignment("abcd:1", "cluster:return", 1.0),),
            representatives_per_cluster=0,
        )
