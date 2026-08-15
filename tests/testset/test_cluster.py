from __future__ import annotations

import pytest

from ses.testset.cluster import (
    ClusterAssignment,
    ClusterContractError,
    ClusterItem,
    SklearnTfidfClusterAdapter,
    assign_clusters,
    compare_cluster_labels,
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
