"""Injectable semantic annotation and deterministic candidate stratification."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Literal, Protocol

from ses.testset.cluster import (
    ClusterAssignment,
    ClusterContractError,
    ClusterDependencyError,
    sklearn_distribution_version,
)
from ses.testset.difficulty import TauDifficulty
from ses.testset.scrub import ScrubbedConversation


class StratifyContractError(ValueError):
    """A stratification adapter returned incomplete or unsafe annotations."""


class CandidateCapacityError(ValueError):
    """A requested candidate cap cannot preserve required label coverage."""


@dataclass(frozen=True)
class StratifyText:
    source_id: str
    text: str


@dataclass(frozen=True)
class TauTaskText:
    task_id: str
    text: str


@dataclass(frozen=True)
class StratifyAnnotation:
    source_id: str
    semantic_group_id: str
    tau_task_id: str | None
    similarity: float | None


class StratifyAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    def annotate(
        self,
        conversations: tuple[StratifyText, ...],
        tau_tasks: tuple[TauTaskText, ...],
        *,
        seed: int,
    ) -> tuple[StratifyAnnotation, ...]: ...


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    source_id: str
    duplicate_source_ids: tuple[str, ...]
    cluster_id: str
    flow: str
    subflow: str
    semantic_group_id: str
    label_frequency: int
    long_tail: bool
    label_conflict: bool
    tau_task_id: str | None
    tau_run_count: int | None
    tau_success_count: int | None
    tau_pass_rate: str | None
    difficulty_bucket: str | None
    similarity: float | None
    retention_reasons: tuple[str, ...]
    executable: Literal[False] = field(default=False, init=False)


@dataclass(frozen=True)
class StratifyFunnel:
    input_records: int
    semantic_duplicates_removed: int
    candidate_pool: int
    candidate_cap_removed: int
    output_candidates: int


@dataclass(frozen=True)
class StratifyResult:
    candidates: tuple[CandidateRecord, ...]
    funnel: StratifyFunnel


def _rank(source_id: str, seed: int) -> str:
    return sha256(f"stratify-v1\0{seed}\0{source_id}".encode()).hexdigest()


def _validate_cluster_assignments(
    records: Sequence[ScrubbedConversation],
    assignments: Sequence[ClusterAssignment],
) -> dict[str, ClusterAssignment]:
    expected = {record.source_id for record in records}
    by_id: dict[str, ClusterAssignment] = {}
    for assignment in assignments:
        if assignment.item_id not in expected:
            raise ClusterContractError(
                f"cluster assignment references unknown record {assignment.item_id}"
            )
        if assignment.item_id in by_id:
            raise ClusterContractError(
                f"duplicate cluster assignment for {assignment.item_id}"
            )
        by_id[assignment.item_id] = assignment
    missing = expected - set(by_id)
    if missing:
        raise ClusterContractError(
            f"missing cluster assignments: {', '.join(sorted(missing))}"
        )
    return by_id


def _validate_annotations(
    records: Sequence[ScrubbedConversation],
    annotations: Sequence[StratifyAnnotation],
    tau_by_id: dict[str, TauDifficulty],
) -> dict[str, StratifyAnnotation]:
    expected = {record.source_id for record in records}
    by_id: dict[str, StratifyAnnotation] = {}
    for annotation in annotations:
        if annotation.source_id not in expected:
            raise StratifyContractError(
                f"annotation references unknown source {annotation.source_id}"
            )
        if annotation.source_id in by_id:
            raise StratifyContractError(
                f"duplicate annotation for {annotation.source_id}"
            )
        if not annotation.semantic_group_id:
            raise StratifyContractError("semantic group ID cannot be empty")
        if (
            annotation.tau_task_id is not None
            and annotation.tau_task_id not in tau_by_id
        ):
            raise StratifyContractError(
                f"annotation references unknown tau task {annotation.tau_task_id}"
            )
        if annotation.similarity is not None and (
            not math.isfinite(annotation.similarity)
            or not 0.0 <= annotation.similarity <= 1.0
        ):
            raise StratifyContractError(
                "semantic similarity must be finite and between 0 and 1"
            )
        if (annotation.tau_task_id is None) != (annotation.similarity is None):
            raise StratifyContractError(
                "tau task and semantic similarity must both be present or absent"
            )
        by_id[annotation.source_id] = annotation
    missing = expected - set(by_id)
    if missing:
        raise StratifyContractError(
            f"missing stratify annotations: {', '.join(sorted(missing))}"
        )
    return by_id


def _candidate_rows(
    records: Sequence[ScrubbedConversation],
    cluster_by_id: dict[str, ClusterAssignment],
    annotation_by_id: dict[str, StratifyAnnotation],
    tau_by_id: dict[str, TauDifficulty],
) -> list[CandidateRecord]:
    label_counts = Counter(record.subflow for record in records)
    average_frequency = len(records) / len(label_counts)
    rows: list[CandidateRecord] = []
    for record in records:
        annotation = annotation_by_id[record.source_id]
        difficulty = (
            tau_by_id[annotation.tau_task_id]
            if annotation.tau_task_id is not None
            else None
        )
        rows.append(
            CandidateRecord(
                candidate_id=f"candidate:{record.source_id}",
                source_id=record.source_id,
                duplicate_source_ids=record.duplicate_source_ids,
                cluster_id=cluster_by_id[record.source_id].cluster_id,
                flow=record.flow,
                subflow=record.subflow,
                semantic_group_id=annotation.semantic_group_id,
                label_frequency=label_counts[record.subflow],
                long_tail=label_counts[record.subflow] < average_frequency,
                label_conflict=record.label_conflict,
                tau_task_id=annotation.tau_task_id,
                tau_run_count=difficulty.run_count if difficulty else None,
                tau_success_count=difficulty.success_count if difficulty else None,
                tau_pass_rate=difficulty.pass_rate_decimal if difficulty else None,
                difficulty_bucket=(
                    difficulty.difficulty_bucket if difficulty else None
                ),
                similarity=annotation.similarity,
                retention_reasons=(),
            )
        )

    grouped: dict[tuple[str, str], list[CandidateRecord]] = defaultdict(list)
    labels_by_semantic_group: dict[str, set[str]] = defaultdict(set)
    tau_tasks_by_semantic_group: dict[str, set[str | None]] = defaultdict(set)
    for row in rows:
        grouped[(row.semantic_group_id, row.subflow)].append(row)
        labels_by_semantic_group[row.semantic_group_id].add(row.subflow)
        tau_tasks_by_semantic_group[row.semantic_group_id].add(row.tau_task_id)
    for semantic_group_id, tau_task_ids in tau_tasks_by_semantic_group.items():
        if len(tau_task_ids) > 1:
            raise StratifyContractError(
                f"semantic group {semantic_group_id} has conflicting tau task annotations"
            )
    deduplicated: list[CandidateRecord] = []
    for key in sorted(grouped):
        group = sorted(grouped[key], key=lambda item: item.source_id)
        representative = group[0]
        semantic_duplicates = {
            duplicate_id for item in group for duplicate_id in item.duplicate_source_ids
        }
        semantic_duplicates.update(item.source_id for item in group[1:])
        deduplicated.append(
            replace(
                representative,
                duplicate_source_ids=tuple(sorted(semantic_duplicates)),
                label_conflict=(
                    any(item.label_conflict for item in group)
                    or len(labels_by_semantic_group[representative.semantic_group_id])
                    > 1
                ),
            )
        )
    return deduplicated


def _number_word(value: int) -> str:
    return {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}.get(value, str(value))


def _select_candidates(
    candidates: Sequence[CandidateRecord], target_count: int, seed: int
) -> tuple[CandidateRecord, ...]:
    labels = sorted({candidate.subflow for candidate in candidates})
    if target_count < len(labels):
        raise CandidateCapacityError(
            f"target_count cannot cover {_number_word(len(labels))} label groups"
        )
    target_count = min(target_count, len(candidates))
    ranked = sorted(
        candidates, key=lambda item: (_rank(item.source_id, seed), item.source_id)
    )
    selected: dict[str, CandidateRecord] = {}

    def add(candidate: CandidateRecord, reason: str) -> None:
        previous = selected.get(candidate.source_id)
        if previous is None:
            selected[candidate.source_id] = replace(
                candidate, retention_reasons=(reason,)
            )
        elif reason not in previous.retention_reasons:
            selected[candidate.source_id] = replace(
                previous,
                retention_reasons=(*previous.retention_reasons, reason),
            )

    ordered_labels = sorted(
        labels, key=lambda value: (Counter(c.subflow for c in candidates)[value], value)
    )
    bucket_order = {"hard": 0, "medium": 1, "easy": 2, None: 3}
    label_floor_states: dict[frozenset[str], tuple[CandidateRecord, ...]] = {
        frozenset(): ()
    }
    for label in ordered_labels:
        first_by_bucket: dict[str | None, CandidateRecord] = {}
        for item in ranked:
            if item.subflow == label:
                first_by_bucket.setdefault(item.difficulty_bucket, item)
        next_states: dict[frozenset[str], tuple[CandidateRecord, ...]] = {}
        for covered_buckets, current in label_floor_states.items():
            for bucket, item in sorted(
                first_by_bucket.items(), key=lambda pair: bucket_order[pair[0]]
            ):
                next_covered = (
                    covered_buckets | {bucket}
                    if bucket in {"hard", "medium", "easy"}
                    else covered_buckets
                )
                proposal = (*current, item)
                previous = next_states.get(frozenset(next_covered))
                if previous is None or tuple(
                    (_rank(value.source_id, seed), value.source_id)
                    for value in proposal
                ) < tuple(
                    (_rank(value.source_id, seed), value.source_id)
                    for value in previous
                ):
                    next_states[frozenset(next_covered)] = proposal
        label_floor_states = next_states
    label_floor = min(
        label_floor_states.items(),
        key=lambda item: (
            -len(item[0]),
            tuple((_rank(value.source_id, seed), value.source_id) for value in item[1]),
        ),
    )[1]
    for item in label_floor:
        add(item, "label_floor")

    available_buckets = {
        item.difficulty_bucket
        for item in candidates
        if item.difficulty_bucket in {"hard", "medium", "easy"}
    }
    selected_buckets = {
        item.difficulty_bucket
        for item in selected.values()
        if item.difficulty_bucket in available_buckets
    }
    minimum_joint_capacity = len(selected) + len(available_buckets - selected_buckets)
    if target_count < minimum_joint_capacity:
        raise CandidateCapacityError(
            "target_count cannot jointly cover every label and available "
            f"difficulty bucket; at least {minimum_joint_capacity} are required"
        )

    for bucket in ("hard", "medium", "easy"):
        if any(item.difficulty_bucket == bucket for item in selected.values()):
            representative = next(
                item for item in selected.values() if item.difficulty_bucket == bucket
            )
            add(representative, "difficulty_floor")
            continue
        if len(selected) >= target_count:
            continue
        match = next(
            (item for item in ranked if item.difficulty_bucket == bucket), None
        )
        if match is not None:
            add(match, "difficulty_floor")

    strata: dict[tuple[str, str], list[CandidateRecord]] = defaultdict(list)
    for item in ranked:
        strata[(item.subflow, item.difficulty_bucket or "unknown")].append(item)
    ordered_strata = sorted(strata)
    while len(selected) < target_count:
        progressed = False
        for stratum in ordered_strata:
            while strata[stratum] and strata[stratum][0].source_id in selected:
                strata[stratum].pop(0)
            if not strata[stratum]:
                continue
            add(strata[stratum].pop(0), "balanced_fill")
            progressed = True
            if len(selected) >= target_count:
                break
        if not progressed:
            break
    return tuple(selected[source_id] for source_id in sorted(selected))


def stratify_candidates(
    records: Sequence[ScrubbedConversation],
    assignments: Sequence[ClusterAssignment],
    tau_difficulty: Sequence[TauDifficulty],
    *,
    adapter: StratifyAdapter,
    target_count: int | None = None,
    seed: int = 0,
) -> StratifyResult:
    """Annotate with tau task references, then retain balanced label coverage."""

    if target_count is not None and target_count < 0:
        raise CandidateCapacityError("target_count cannot be negative")
    if not records:
        return StratifyResult(
            candidates=(),
            funnel=StratifyFunnel(
                input_records=0,
                semantic_duplicates_removed=0,
                candidate_pool=0,
                candidate_cap_removed=0,
                output_candidates=0,
            ),
        )
    cluster_by_id = _validate_cluster_assignments(records, assignments)
    tau_by_id = {summary.task_id: summary for summary in tau_difficulty}
    if len(tau_by_id) != len(tau_difficulty):
        raise StratifyContractError("duplicate tau task summaries")
    conversations = tuple(
        StratifyText(source_id=record.source_id, text=record.normalized_text)
        for record in sorted(records, key=lambda item: item.source_id)
    )
    tau_tasks = tuple(
        TauTaskText(task_id=summary.task_id, text=summary.task_text)
        for summary in sorted(tau_difficulty, key=lambda item: item.task_id)
    )
    annotations = tuple(adapter.annotate(conversations, tau_tasks, seed=seed))
    annotation_by_id = _validate_annotations(records, annotations, tau_by_id)
    candidates = _candidate_rows(records, cluster_by_id, annotation_by_id, tau_by_id)
    resolved_target = len(candidates) if target_count is None else target_count
    selected = _select_candidates(candidates, resolved_target, seed)
    return StratifyResult(
        candidates=selected,
        funnel=StratifyFunnel(
            input_records=len(records),
            semantic_duplicates_removed=len(records) - len(candidates),
            candidate_pool=len(candidates),
            candidate_cap_removed=len(candidates) - len(selected),
            output_candidates=len(selected),
        ),
    )


class SklearnCosineStratifyAdapter:
    """Local TF-IDF cosine mapping from ABCD language to tau2 task signals."""

    def __init__(
        self,
        *,
        minimum_similarity: float = 0.05,
        semantic_duplicate_similarity: float = 0.9,
    ) -> None:
        if not 0 <= minimum_similarity <= semantic_duplicate_similarity <= 1:
            raise ValueError(
                "similarity thresholds must satisfy 0 <= min <= dedup <= 1"
            )
        self.minimum_similarity = minimum_similarity
        self.semantic_duplicate_similarity = semantic_duplicate_similarity

    @property
    def adapter_id(self) -> str:
        return (
            "sklearn-tfidf-cosine:v1"
            f":scikit_learn={sklearn_distribution_version()}"
            f":minimum_similarity={self.minimum_similarity}"
            f":semantic_duplicate_similarity={self.semantic_duplicate_similarity}"
            ":max_features=20000:ngram_range=1-2"
        )

    def annotate(
        self,
        conversations: tuple[StratifyText, ...],
        tau_tasks: tuple[TauTaskText, ...],
        *,
        seed: int,
    ) -> tuple[StratifyAnnotation, ...]:
        del seed
        if not tau_tasks:
            return tuple(
                StratifyAnnotation(
                    source_id=item.source_id,
                    semantic_group_id=f"source:{item.source_id}",
                    tau_task_id=None,
                    similarity=None,
                )
                for item in conversations
            )
        try:
            from sklearn.feature_extraction.text import (  # type: ignore[import-untyped]
                TfidfVectorizer,
            )
            from sklearn.metrics.pairwise import (  # type: ignore[import-untyped]
                cosine_similarity,
            )
        except ImportError as exc:
            raise ClusterDependencyError(
                "install the testset optional dependency for local stratification"
            ) from exc
        texts = [item.text for item in conversations] + [
            item.text for item in tau_tasks
        ]
        matrix = TfidfVectorizer(
            lowercase=True,
            ngram_range=(1, 2),
            max_features=20_000,
        ).fit_transform(texts)
        similarities = cosine_similarity(
            matrix[: len(conversations)], matrix[len(conversations) :]
        )
        annotations: list[StratifyAnnotation] = []
        for index, item in enumerate(conversations):
            best_index = int(similarities[index].argmax())
            score = float(similarities[index, best_index])
            if score < self.minimum_similarity:
                task_id = None
                similarity = None
                semantic_group = f"source:{item.source_id}"
            else:
                task_id = tau_tasks[best_index].task_id
                similarity = score
                semantic_group = (
                    f"tau:{task_id}"
                    if score >= self.semantic_duplicate_similarity
                    else f"source:{item.source_id}"
                )
            annotations.append(
                StratifyAnnotation(
                    source_id=item.source_id,
                    semantic_group_id=semantic_group,
                    tau_task_id=task_id,
                    similarity=similarity,
                )
            )
        return tuple(annotations)
