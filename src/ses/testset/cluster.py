"""Injectable local clustering, representatives, and external-label metrics."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol


class ClusterContractError(ValueError):
    """A cluster adapter violated the small, deterministic adapter contract."""


class ClusterDependencyError(RuntimeError):
    """An optional local clustering dependency is not installed."""


CONFIDENCE_QUANTIZATION_DECIMALS = 12
_CONFIDENCE_QUANTUM = Decimal("0.000000000001")


def _canonical_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    quantized = Decimal(str(value)).quantize(
        _CONFIDENCE_QUANTUM,
        rounding=ROUND_HALF_EVEN,
    )
    result = float(quantized)
    return 0.0 if result == 0.0 else result


def sklearn_distribution_version() -> str:
    """Return the installed local adapter version for artifact provenance."""

    try:
        return version("scikit-learn")
    except PackageNotFoundError:
        return "not-installed"


@dataclass(frozen=True)
class ClusterItem:
    item_id: str
    text: str
    source_kind: str


@dataclass(frozen=True)
class ClusterAssignment:
    item_id: str
    cluster_id: str
    confidence: float | None = None


@dataclass(frozen=True)
class ClusterRepresentativeSample:
    """A self-contained, auditable example selected from one cluster."""

    rank: int
    item_id: str
    text: str
    source_kind: str
    confidence: float | None
    selection_reason: str


@dataclass(frozen=True)
class ClusterSummary:
    """Deterministic cluster-level audit information."""

    cluster_id: str
    member_count: int
    representative_selection_method: str
    representative_samples: tuple[ClusterRepresentativeSample, ...]


class ClusterAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    def cluster(
        self, items: tuple[ClusterItem, ...]
    ) -> tuple[ClusterAssignment, ...]: ...


@dataclass(frozen=True)
class ContingencyCell:
    reference_label: str
    cluster_id: str
    count: int


@dataclass(frozen=True)
class LabelComparison:
    label_name: str
    evaluated_count: int
    excluded_missing_label_count: int
    true_label_count: int
    cluster_count: int
    contingency: tuple[ContingencyCell, ...]
    adjusted_rand_index: float
    normalized_mutual_info: float
    homogeneity: float
    completeness: float
    v_measure: float
    informative: bool
    reason: str | None


def _stable_cluster_id(member_ids: Sequence[str]) -> str:
    payload = "\0".join(sorted(member_ids)).encode("utf-8")
    return f"cluster:{sha256(payload).hexdigest()[:16]}"


def assign_clusters(
    items: Sequence[ClusterItem], adapter: ClusterAdapter
) -> tuple[ClusterAssignment, ...]:
    """Validate adapter output and canonicalize labels from cluster membership."""

    frozen_items = tuple(items)
    if not frozen_items:
        raise ClusterContractError("cannot cluster an empty input")
    item_ids = [item.item_id for item in frozen_items]
    if any(not item_id for item_id in item_ids) or len(item_ids) != len(set(item_ids)):
        raise ClusterContractError("cluster item IDs must be unique and non-empty")

    raw_assignments = tuple(adapter.cluster(frozen_items))
    expected = set(item_ids)
    by_id: dict[str, ClusterAssignment] = {}
    for assignment in raw_assignments:
        if assignment.item_id not in expected:
            raise ClusterContractError(f"unknown assignment: {assignment.item_id}")
        if assignment.item_id in by_id:
            raise ClusterContractError(f"duplicate assignment: {assignment.item_id}")
        if not assignment.cluster_id:
            raise ClusterContractError("cluster ID cannot be empty")
        if assignment.confidence is not None and (
            not math.isfinite(assignment.confidence)
            or not 0.0 <= assignment.confidence <= 1.0
        ):
            raise ClusterContractError("confidence must be finite and between 0 and 1")
        by_id[assignment.item_id] = assignment
    missing = sorted(expected - set(by_id))
    if missing:
        raise ClusterContractError(f"missing assignments: {', '.join(missing)}")

    members_by_raw_cluster: dict[str, list[str]] = defaultdict(list)
    for assignment in by_id.values():
        members_by_raw_cluster[assignment.cluster_id].append(assignment.item_id)
    canonical = {
        raw_cluster: _stable_cluster_id(members)
        for raw_cluster, members in members_by_raw_cluster.items()
    }
    return tuple(
        ClusterAssignment(
            item_id=item_id,
            cluster_id=canonical[by_id[item_id].cluster_id],
            confidence=_canonical_confidence(by_id[item_id].confidence),
        )
        for item_id in item_ids
    )


REPRESENTATIVE_SELECTION_METHOD = (
    "assignment_confidence_desc_nulls_last_then_item_id_asc"
)


def summarize_clusters(
    items: Sequence[ClusterItem],
    assignments: Sequence[ClusterAssignment],
    *,
    representatives_per_cluster: int = 3,
) -> tuple[ClusterSummary, ...]:
    """Select stable representative samples without depending on input order.

    Assignment confidence is the adapter's explicit signal for how well an item fits
    its cluster. Missing confidence sorts after present confidence, and ``item_id``
    provides the deterministic final tie-break. The returned summaries and samples
    are both canonically ordered.
    """

    if representatives_per_cluster < 1:
        raise ValueError("representatives_per_cluster must be positive")

    item_by_id: dict[str, ClusterItem] = {}
    for item in items:
        if not item.item_id:
            raise ClusterContractError("cluster item ID cannot be empty")
        if item.item_id in item_by_id:
            raise ClusterContractError(f"duplicate cluster item: {item.item_id}")
        item_by_id[item.item_id] = item
    if not item_by_id:
        raise ClusterContractError("cannot summarize an empty input")

    assignment_by_id: dict[str, ClusterAssignment] = {}
    for assignment in assignments:
        if assignment.item_id not in item_by_id:
            raise ClusterContractError(f"unknown assignment: {assignment.item_id}")
        if assignment.item_id in assignment_by_id:
            raise ClusterContractError(f"duplicate assignment: {assignment.item_id}")
        if not assignment.cluster_id:
            raise ClusterContractError("cluster ID cannot be empty")
        if assignment.confidence is not None and (
            not math.isfinite(assignment.confidence)
            or not 0.0 <= assignment.confidence <= 1.0
        ):
            raise ClusterContractError("confidence must be finite and between 0 and 1")
        assignment_by_id[assignment.item_id] = assignment

    missing = sorted(set(item_by_id) - set(assignment_by_id))
    if missing:
        raise ClusterContractError(f"missing assignments: {', '.join(missing)}")

    assignments_by_cluster: dict[str, list[ClusterAssignment]] = defaultdict(list)
    for assignment in assignment_by_id.values():
        assignments_by_cluster[assignment.cluster_id].append(assignment)

    summaries: list[ClusterSummary] = []
    for cluster_id, members in sorted(assignments_by_cluster.items()):
        ranked = sorted(
            members,
            key=lambda assignment: (
                assignment.confidence is None,
                -(assignment.confidence or 0.0),
                assignment.item_id,
            ),
        )
        representative_samples = tuple(
            ClusterRepresentativeSample(
                rank=rank,
                item_id=assignment.item_id,
                text=item_by_id[assignment.item_id].text,
                source_kind=item_by_id[assignment.item_id].source_kind,
                confidence=assignment.confidence,
                selection_reason=(
                    f"rank={rank};method={REPRESENTATIVE_SELECTION_METHOD}"
                ),
            )
            for rank, assignment in enumerate(
                ranked[:representatives_per_cluster], start=1
            )
        )
        summaries.append(
            ClusterSummary(
                cluster_id=cluster_id,
                member_count=len(members),
                representative_selection_method=REPRESENTATIVE_SELECTION_METHOD,
                representative_samples=representative_samples,
            )
        )
    return tuple(summaries)


def _rounded(value: float) -> float:
    rounded = round(value, 12)
    return 0.0 if rounded == 0.0 else rounded


def compare_cluster_labels(
    assignments: Sequence[ClusterAssignment],
    labels_by_item_id: dict[str, str | None],
    *,
    label_name: str,
) -> LabelComparison:
    """Compare a partition with existing labels using ARI and V-measure family metrics."""

    labeled: list[tuple[str, str]] = []
    excluded = 0
    for assignment in assignments:
        label = labels_by_item_id.get(assignment.item_id)
        if label is None or not label:
            excluded += 1
            continue
        labeled.append((label, assignment.cluster_id))
    if not labeled:
        raise ClusterContractError(f"no labeled records for {label_name}")

    cell_counts = Counter(labeled)
    reference_counts = Counter(label for label, _ in labeled)
    cluster_counts = Counter(cluster for _, cluster in labeled)
    total = len(labeled)
    try:
        from sklearn.metrics import (
            adjusted_rand_score,
            homogeneity_completeness_v_measure,
            normalized_mutual_info_score,
        )
    except ImportError as exc:
        raise ClusterDependencyError(
            "install the testset optional dependency to compare cluster labels"
        ) from exc
    reference_labels = [reference for reference, _ in labeled]
    cluster_labels = [cluster for _, cluster in labeled]
    adjusted_rand = float(adjusted_rand_score(reference_labels, cluster_labels))
    normalized_mutual_info = float(
        normalized_mutual_info_score(
            reference_labels,
            cluster_labels,
            average_method="arithmetic",
        )
    )
    homogeneity, completeness, v_measure = (
        float(value)
        for value in homogeneity_completeness_v_measure(
            reference_labels,
            cluster_labels,
        )
    )
    informative = len(reference_counts) > 1
    return LabelComparison(
        label_name=label_name,
        evaluated_count=total,
        excluded_missing_label_count=excluded,
        true_label_count=len(reference_counts),
        cluster_count=len(cluster_counts),
        contingency=tuple(
            ContingencyCell(
                reference_label=reference_label,
                cluster_id=cluster_id,
                count=count,
            )
            for (reference_label, cluster_id), count in sorted(cell_counts.items())
        ),
        adjusted_rand_index=_rounded(adjusted_rand),
        normalized_mutual_info=_rounded(normalized_mutual_info),
        homogeneity=_rounded(homogeneity),
        completeness=_rounded(completeness),
        v_measure=_rounded(v_measure),
        informative=informative,
        reason=None if informative else "single_reference_label",
    )


class SklearnTfidfClusterAdapter:
    """Local TF-IDF + k-means adapter; imports the optional extra lazily."""

    def __init__(
        self,
        *,
        n_clusters: int = 12,
        random_state: int = 0,
        max_features: int = 20_000,
    ) -> None:
        if n_clusters < 1:
            raise ValueError("n_clusters must be positive")
        self.n_clusters = n_clusters
        self.random_state = random_state
        self.max_features = max_features

    @property
    def adapter_id(self) -> str:
        return (
            "sklearn-tfidf-kmeans:v2"
            f":scikit_learn={sklearn_distribution_version()}"
            f":n_clusters={self.n_clusters}"
            f":random_state={self.random_state}"
            f":max_features={self.max_features}"
            ":ngram_range=1-2:n_init=10:confidence_quantization=12dp"
        )

    def cluster(self, items: tuple[ClusterItem, ...]) -> tuple[ClusterAssignment, ...]:
        try:
            from sklearn.cluster import KMeans
            from sklearn.feature_extraction.text import (
                TfidfVectorizer,
            )
        except ImportError as exc:
            raise ClusterDependencyError(
                "install the testset optional dependency to use local TF-IDF clustering"
            ) from exc
        if not items:
            return ()
        cluster_count = min(self.n_clusters, len(items))
        matrix = TfidfVectorizer(
            lowercase=True,
            ngram_range=(1, 2),
            max_features=self.max_features,
        ).fit_transform(item.text for item in items)
        model = KMeans(
            n_clusters=cluster_count,
            random_state=self.random_state,
            n_init=10,
        )
        labels = model.fit_predict(matrix)
        distances = model.transform(matrix)
        return tuple(
            ClusterAssignment(
                item_id=item.item_id,
                cluster_id=str(int(label)),
                confidence=1.0 / (1.0 + float(distances[index, int(label)])),
            )
            for index, (item, label) in enumerate(zip(items, labels, strict=True))
        )
