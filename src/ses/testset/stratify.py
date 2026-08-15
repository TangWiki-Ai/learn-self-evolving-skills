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
class AbcdPairSimilarity:
    """A direct ABCD-to-ABCD semantic similarity measurement."""

    source_id: str
    duplicate_source_id: str
    similarity: float


@dataclass(frozen=True)
class TauTaskMatch:
    """An independent ABCD-to-tau2 task match used for difficulty provenance."""

    source_id: str
    tau_task_id: str
    similarity: float


class StratifyAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    @property
    def semantic_duplicate_similarity(self) -> float: ...

    def compare_abcd(
        self,
        conversations: tuple[StratifyText, ...],
        *,
        seed: int,
    ) -> tuple[AbcdPairSimilarity, ...]: ...

    def match_tau(
        self,
        conversations: tuple[StratifyText, ...],
        tau_tasks: tuple[TauTaskText, ...],
        *,
        seed: int,
    ) -> tuple[TauTaskMatch, ...]: ...


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


def _validate_pair_similarities(
    records: Sequence[ScrubbedConversation],
    pairs: Sequence[AbcdPairSimilarity],
) -> tuple[AbcdPairSimilarity, ...]:
    expected = {record.source_id for record in records}
    expected_pairs = {
        (left, right) for left in expected for right in expected if left < right
    }
    by_source_pair: dict[tuple[str, str], AbcdPairSimilarity] = {}
    for pair in pairs:
        if pair.source_id not in expected:
            raise StratifyContractError(
                f"ABCD pair references unknown source {pair.source_id}"
            )
        if pair.duplicate_source_id not in expected:
            raise StratifyContractError(
                f"ABCD pair references unknown source {pair.duplicate_source_id}"
            )
        if pair.source_id == pair.duplicate_source_id:
            raise StratifyContractError("ABCD pair cannot reference itself")
        if not math.isfinite(pair.similarity) or not 0.0 <= pair.similarity <= 1.0:
            raise StratifyContractError(
                "ABCD pair similarity must be finite and between 0 and 1"
            )
        left_id, right_id = sorted((pair.source_id, pair.duplicate_source_id))
        canonical_ids = (left_id, right_id)
        if canonical_ids in by_source_pair:
            raise StratifyContractError(
                "duplicate ABCD pair annotation for " + " and ".join(canonical_ids)
            )
        by_source_pair[canonical_ids] = AbcdPairSimilarity(
            source_id=canonical_ids[0],
            duplicate_source_id=canonical_ids[1],
            similarity=pair.similarity,
        )
    missing = expected_pairs - set(by_source_pair)
    if missing:
        formatted = ", ".join(f"{left}<->{right}" for left, right in sorted(missing))
        raise StratifyContractError(f"missing ABCD pair similarities: {formatted}")
    return tuple(by_source_pair[key] for key in sorted(by_source_pair))


def _semantic_group_by_source(
    records: Sequence[ScrubbedConversation],
    pairs: Sequence[AbcdPairSimilarity],
    *,
    duplicate_threshold: float,
) -> dict[str, str]:
    source_ids = sorted(record.source_id for record in records)
    index_by_source = {source_id: index for index, source_id in enumerate(source_ids)}
    parents = list(range(len(source_ids)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    for pair in pairs:
        if pair.similarity >= duplicate_threshold:
            union(
                index_by_source[pair.source_id],
                index_by_source[pair.duplicate_source_id],
            )

    members_by_root: dict[int, list[str]] = defaultdict(list)
    for source_id, index in index_by_source.items():
        members_by_root[find(index)].append(source_id)
    group_by_source: dict[str, str] = {}
    for members in members_by_root.values():
        canonical_members = tuple(sorted(members))
        digest = sha256(
            b"stratify-abcd-semantic-v1\0" + "\0".join(canonical_members).encode()
        ).hexdigest()
        for source_id in canonical_members:
            group_by_source[source_id] = f"abcd-semantic:{digest}"
    return group_by_source


def _validate_tau_matches(
    records: Sequence[ScrubbedConversation],
    matches: Sequence[TauTaskMatch],
    tau_by_id: dict[str, TauDifficulty],
) -> dict[str, TauTaskMatch]:
    expected = {record.source_id for record in records}
    by_id: dict[str, TauTaskMatch] = {}
    for match in matches:
        if match.source_id not in expected:
            raise StratifyContractError(
                f"tau match references unknown source {match.source_id}"
            )
        if match.source_id in by_id:
            raise StratifyContractError(f"duplicate tau match for {match.source_id}")
        if match.tau_task_id not in tau_by_id:
            raise StratifyContractError(
                f"tau match references unknown tau task {match.tau_task_id}"
            )
        if not math.isfinite(match.similarity) or not 0.0 <= match.similarity <= 1.0:
            raise StratifyContractError(
                "tau task similarity must be finite and between 0 and 1"
            )
        by_id[match.source_id] = match
    return by_id


def _candidate_rows(
    records: Sequence[ScrubbedConversation],
    cluster_by_id: dict[str, ClusterAssignment],
    semantic_group_by_source: dict[str, str],
) -> list[CandidateRecord]:
    label_counts = Counter(record.subflow for record in records)
    average_frequency = len(records) / len(label_counts)
    rows: list[CandidateRecord] = []
    for record in records:
        rows.append(
            CandidateRecord(
                candidate_id=f"candidate:{record.source_id}",
                source_id=record.source_id,
                duplicate_source_ids=record.duplicate_source_ids,
                cluster_id=cluster_by_id[record.source_id].cluster_id,
                flow=record.flow,
                subflow=record.subflow,
                semantic_group_id=semantic_group_by_source[record.source_id],
                label_frequency=label_counts[record.subflow],
                long_tail=label_counts[record.subflow] < average_frequency,
                label_conflict=record.label_conflict,
                tau_task_id=None,
                tau_run_count=None,
                tau_success_count=None,
                tau_pass_rate=None,
                difficulty_bucket=None,
                similarity=None,
                retention_reasons=(),
            )
        )

    return rows


def _attach_tau_difficulty(
    candidates: Sequence[CandidateRecord],
    tau_match_by_source: dict[str, TauTaskMatch],
    tau_by_id: dict[str, TauDifficulty],
) -> list[CandidateRecord]:
    attached: list[CandidateRecord] = []
    for candidate in candidates:
        tau_match = tau_match_by_source.get(candidate.source_id)
        if tau_match is None:
            attached.append(candidate)
            continue
        difficulty = tau_by_id[tau_match.tau_task_id]
        attached.append(
            replace(
                candidate,
                tau_task_id=tau_match.tau_task_id,
                tau_run_count=difficulty.run_count,
                tau_success_count=difficulty.success_count,
                tau_pass_rate=difficulty.pass_rate_decimal,
                difficulty_bucket=difficulty.difficulty_bucket,
                similarity=tau_match.similarity,
            )
        )
    return attached


def _deduplicate_abcd_candidates(
    rows: Sequence[CandidateRecord],
) -> list[CandidateRecord]:
    """Collapse ABCD semantic matches without using tau2 task annotations."""

    grouped: dict[tuple[str, str], list[CandidateRecord]] = defaultdict(list)
    labels_by_semantic_group: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        grouped[(row.semantic_group_id, row.subflow)].append(row)
        labels_by_semantic_group[row.semantic_group_id].add(row.subflow)
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
    """Deduplicate ABCD records, attach tau signals, then balance candidates."""

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
    duplicate_threshold = adapter.semantic_duplicate_similarity
    if not math.isfinite(duplicate_threshold) or not 0 <= duplicate_threshold <= 1:
        raise StratifyContractError(
            "semantic duplicate similarity must be finite and between 0 and 1"
        )
    pair_similarities = _validate_pair_similarities(
        records,
        adapter.compare_abcd(conversations, seed=seed),
    )
    semantic_groups = _semantic_group_by_source(
        records,
        pair_similarities,
        duplicate_threshold=duplicate_threshold,
    )
    rows = _candidate_rows(records, cluster_by_id, semantic_groups)
    deduplicated_candidates = _deduplicate_abcd_candidates(rows)
    text_by_source = {
        conversation.source_id: conversation.text for conversation in conversations
    }
    candidate_source_ids = {
        candidate.source_id for candidate in deduplicated_candidates
    }
    candidate_conversations = tuple(
        StratifyText(
            source_id=candidate.source_id,
            text=text_by_source[candidate.source_id],
        )
        for candidate in sorted(
            deduplicated_candidates, key=lambda item: item.source_id
        )
    )
    tau_matches = adapter.match_tau(candidate_conversations, tau_tasks, seed=seed)
    tau_match_by_source = _validate_tau_matches(
        tuple(record for record in records if record.source_id in candidate_source_ids),
        tau_matches,
        tau_by_id,
    )
    candidates = _attach_tau_difficulty(
        deduplicated_candidates,
        tau_match_by_source,
        tau_by_id,
    )
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
    """Local TF-IDF ABCD deduplication and independent tau2 task mapping."""

    def __init__(
        self,
        *,
        minimum_similarity: float = 0.05,
        semantic_duplicate_similarity: float = 0.9,
    ) -> None:
        if not 0 <= minimum_similarity <= 1:
            raise ValueError("minimum similarity must be between 0 and 1")
        if not 0 <= semantic_duplicate_similarity <= 1:
            raise ValueError("semantic duplicate similarity must be between 0 and 1")
        self.minimum_similarity = minimum_similarity
        self.semantic_duplicate_similarity = semantic_duplicate_similarity

    @property
    def adapter_id(self) -> str:
        return (
            "sklearn-tfidf-cosine:v2"
            f":scikit_learn={sklearn_distribution_version()}"
            f":minimum_similarity={self.minimum_similarity}"
            f":semantic_duplicate_similarity={self.semantic_duplicate_similarity}"
            ":max_features=20000:ngram_range=1-2:token_pattern=unicode-word"
        )

    def compare_abcd(
        self,
        conversations: tuple[StratifyText, ...],
        *,
        seed: int,
    ) -> tuple[AbcdPairSimilarity, ...]:
        del seed
        if len(conversations) < 2:
            return ()
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
        ordered_conversations = tuple(
            sorted(conversations, key=lambda item: item.source_id)
        )
        conversation_matrix = TfidfVectorizer(
            lowercase=True,
            ngram_range=(1, 2),
            max_features=20_000,
            token_pattern=r"(?u)\b\w+\b",
        ).fit_transform([item.text for item in ordered_conversations])
        abcd_similarities = cosine_similarity(conversation_matrix)
        return tuple(
            AbcdPairSimilarity(
                source_id=ordered_conversations[left].source_id,
                duplicate_source_id=ordered_conversations[right].source_id,
                similarity=float(abcd_similarities[left, right]),
            )
            for left in range(len(ordered_conversations))
            for right in range(left + 1, len(ordered_conversations))
        )

    def match_tau(
        self,
        conversations: tuple[StratifyText, ...],
        tau_tasks: tuple[TauTaskText, ...],
        *,
        seed: int,
    ) -> tuple[TauTaskMatch, ...]:
        del seed
        if not conversations or not tau_tasks:
            return ()
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
        ordered_conversations = tuple(
            sorted(conversations, key=lambda item: item.source_id)
        )
        ordered_tau_tasks = tuple(sorted(tau_tasks, key=lambda item: item.task_id))
        matrix = TfidfVectorizer(
            lowercase=True,
            ngram_range=(1, 2),
            max_features=20_000,
            token_pattern=r"(?u)\b\w+\b",
        ).fit_transform(
            [item.text for item in ordered_conversations]
            + [item.text for item in ordered_tau_tasks]
        )
        similarities = cosine_similarity(
            matrix[: len(ordered_conversations)],
            matrix[len(ordered_conversations) :],
        )
        matches: list[TauTaskMatch] = []
        for index, item in enumerate(ordered_conversations):
            best_index = int(similarities[index].argmax())
            score = float(similarities[index, best_index])
            if score >= self.minimum_similarity:
                matches.append(
                    TauTaskMatch(
                        source_id=item.source_id,
                        tau_task_id=ordered_tau_tasks[best_index].task_id,
                        similarity=score,
                    )
                )
        return tuple(matches)
