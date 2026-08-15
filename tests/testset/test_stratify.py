from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256

import pytest

from ses.testset.cluster import ClusterAssignment
from ses.testset.difficulty import TauDifficulty
from ses.testset.scrub import ScrubbedConversation, scrub_abcd
from ses.testset.stratify import (
    AbcdPairSimilarity,
    CandidateCapacityError,
    SklearnCosineStratifyAdapter,
    StratifyContractError,
    StratifyText,
    TauTaskMatch,
    TauTaskText,
    stratify_candidates,
)


def raw_record(convo_id: int, subflow: str) -> dict[str, object]:
    return {
        "convo_id": convo_id,
        "source_split": "fixture",
        "scenario": {"flow": "product_defect", "subflow": subflow},
        "original": [["customer", f"request {convo_id} for {subflow}"]],
        "delexed": [
            {
                "speaker": "customer",
                "text": f"request {convo_id} for {subflow}",
                "turn_count": 1,
            }
        ],
    }


def tau_summary(task_id: str, bucket: str, success_count: int) -> TauDifficulty:
    return TauDifficulty(
        source_id=f"tau2:commit:{task_id}",
        task_id=task_id,
        task_text=f"tau task {task_id}",
        run_count=16,
        success_count=success_count,
        pass_rate=success_count / 16,
        pass_rate_decimal=str(success_count / 16),
        mean_reward=success_count / 16,
        difficulty_score=1 - (success_count / 16),
        difficulty_bucket=bucket,
        per_asset=(),
        generation_commits=("generation-commit",),
    )


class DeterministicFakeStratifyAdapter:
    def __init__(
        self,
        task_by_source: Mapping[str, str | None],
        duplicate_pairs: set[frozenset[str]] | None = None,
    ) -> None:
        self.task_by_source = task_by_source
        self.duplicate_pairs = duplicate_pairs or set()
        self.seen_conversations: tuple[StratifyText, ...] = ()
        self.seen_tau_conversations: tuple[StratifyText, ...] = ()
        self.seen_tasks: tuple[TauTaskText, ...] = ()

    @property
    def adapter_id(self) -> str:
        return "deterministic-fake-stratify:v1"

    @property
    def semantic_duplicate_similarity(self) -> float:
        return 0.9

    def compare_abcd(
        self,
        conversations: tuple[StratifyText, ...],
        *,
        seed: int,
    ) -> tuple[AbcdPairSimilarity, ...]:
        del seed
        self.seen_conversations = conversations
        reversed_conversations = tuple(reversed(conversations))
        return tuple(
            AbcdPairSimilarity(
                source_id=left.source_id,
                duplicate_source_id=right.source_id,
                similarity=(
                    1.0
                    if frozenset((left.source_id, right.source_id))
                    in self.duplicate_pairs
                    else 0.0
                ),
            )
            for left_index, left in enumerate(reversed_conversations)
            for right in reversed_conversations[left_index + 1 :]
        )

    def match_tau(
        self,
        conversations: tuple[StratifyText, ...],
        tau_tasks: tuple[TauTaskText, ...],
        *,
        seed: int,
    ) -> tuple[TauTaskMatch, ...]:
        del seed
        self.seen_tau_conversations = conversations
        self.seen_tasks = tau_tasks
        matches: list[TauTaskMatch] = []
        for item in reversed(conversations):
            task_id = self.task_by_source[item.source_id]
            if task_id is not None:
                matches.append(
                    TauTaskMatch(
                        source_id=item.source_id,
                        tau_task_id=task_id,
                        similarity=1.0,
                    )
                )
        return tuple(matches)


def scrubbed_records() -> tuple[ScrubbedConversation, ...]:
    records = [
        raw_record(1, "common"),
        raw_record(2, "common"),
        raw_record(3, "common"),
        raw_record(4, "secondary"),
        raw_record(5, "secondary"),
        raw_record(6, "rare"),
    ]
    return scrub_abcd(records).records


def test_stratification_joins_task_level_difficulty_and_preserves_long_tail() -> None:
    records = scrubbed_records()
    assignments = tuple(
        ClusterAssignment(record.source_id, f"cluster:{record.subflow}", 1.0)
        for record in records
    )
    task_by_source = {
        record.source_id: ("hard" if record.subflow == "rare" else "easy")
        for record in records
    }
    adapter = DeterministicFakeStratifyAdapter(task_by_source)

    first = stratify_candidates(
        records,
        assignments,
        (tau_summary("hard", "hard", 2), tau_summary("easy", "easy", 14)),
        adapter=adapter,
        target_count=3,
        seed=7,
    ).candidates
    second = stratify_candidates(
        tuple(reversed(records)),
        tuple(reversed(assignments)),
        (tau_summary("easy", "easy", 14), tau_summary("hard", "hard", 2)),
        adapter=DeterministicFakeStratifyAdapter(task_by_source),
        target_count=3,
        seed=7,
    ).candidates

    assert [candidate.source_id for candidate in first] == [
        candidate.source_id for candidate in second
    ]
    assert {candidate.subflow for candidate in first} == {
        "common",
        "secondary",
        "rare",
    }
    rare = next(candidate for candidate in first if candidate.subflow == "rare")
    assert rare.long_tail is True
    assert rare.difficulty_bucket == "hard"
    assert rare.tau_run_count == 16
    assert "label_floor" in rare.retention_reasons
    assert all(candidate.executable is False for candidate in first)
    forbidden = {"creator", "selection", "final", "gold", "shop_state"}
    assert all(not (set(asdict(candidate)) & forbidden) for candidate in first)
    # The adapter receives task text only, never the 1,824 trajectory rows or rewards.
    assert len(adapter.seen_tasks) == 2
    assert not hasattr(adapter.seen_tasks[0], "success_count")


def test_stratification_rejects_capacity_that_would_drop_a_label() -> None:
    records = scrubbed_records()
    assignments = tuple(
        ClusterAssignment(record.source_id, "cluster", 1.0) for record in records
    )
    adapter = DeterministicFakeStratifyAdapter(
        {record.source_id: None for record in records}
    )

    with pytest.raises(CandidateCapacityError, match="three label groups"):
        stratify_candidates(
            records,
            assignments,
            (),
            adapter=adapter,
            target_count=2,
        )


def test_empty_stratification_still_rejects_negative_capacity() -> None:
    with pytest.raises(CandidateCapacityError, match="cannot be negative"):
        stratify_candidates(
            (),
            (),
            (),
            adapter=DeterministicFakeStratifyAdapter({}),
            target_count=-1,
        )


def test_label_floor_chooses_a_feasible_mix_of_difficulty_buckets() -> None:
    records = scrub_abcd(
        [
            raw_record(10, "label-a"),
            raw_record(11, "label-a"),
            raw_record(20, "label-b"),
            raw_record(21, "label-b"),
            raw_record(30, "label-c"),
            raw_record(31, "label-c"),
        ]
    ).records
    assignments = tuple(
        ClusterAssignment(record.source_id, f"cluster:{record.subflow}", 1.0)
        for record in records
    )

    def rank(source_id: str) -> str:
        return sha256(f"stratify-v1\0{0}\0{source_id}".encode()).hexdigest()

    task_by_source: dict[str, str | None] = {}
    alternate_bucket = {"label-a": "hard", "label-b": "medium", "label-c": "easy"}
    for label, alternate in alternate_bucket.items():
        members = sorted(
            (record for record in records if record.subflow == label),
            key=lambda item: rank(item.source_id),
        )
        task_by_source[members[0].source_id] = "easy"
        task_by_source[members[1].source_id] = alternate

    selected = stratify_candidates(
        records,
        assignments,
        (
            tau_summary("hard", "hard", 2),
            tau_summary("medium", "medium", 8),
            tau_summary("easy", "easy", 14),
        ),
        adapter=DeterministicFakeStratifyAdapter(task_by_source),
        target_count=3,
    ).candidates

    assert {candidate.subflow for candidate in selected} == {
        "label-a",
        "label-b",
        "label-c",
    }
    assert {candidate.difficulty_bucket for candidate in selected} == {
        "hard",
        "medium",
        "easy",
    }
    assert all("difficulty_floor" in item.retention_reasons for item in selected)


def test_abcd_near_duplicates_merge_and_preserve_every_duplicate_id() -> None:
    records = scrub_abcd(
        [
            {
                **raw_record(50, "return_size"),
                "original": [["customer", "same request::first wording"]],
                "delexed": [
                    {
                        "speaker": "customer",
                        "text": "same request::first wording",
                        "turn_count": 1,
                    }
                ],
            },
            {
                **raw_record(51, "return_size"),
                "original": [["customer", "same request::first wording"]],
                "delexed": [
                    {
                        "speaker": "customer",
                        "text": "same request::first wording",
                        "turn_count": 1,
                    }
                ],
            },
            {
                **raw_record(52, "return_size"),
                "original": [["customer", "same request::alternate wording"]],
                "delexed": [
                    {
                        "speaker": "customer",
                        "text": "same request::alternate wording",
                        "turn_count": 1,
                    }
                ],
            },
            {
                **raw_record(53, "return_size"),
                "original": [["customer", "same request::alternate wording"]],
                "delexed": [
                    {
                        "speaker": "customer",
                        "text": "same request::alternate wording",
                        "turn_count": 1,
                    }
                ],
            },
        ]
    ).records
    assignments = tuple(
        ClusterAssignment(record.source_id, "cluster:return", 1.0) for record in records
    )
    task_by_source = {record.source_id: None for record in records}

    result = stratify_candidates(
        records,
        assignments,
        (),
        adapter=DeterministicFakeStratifyAdapter(
            task_by_source,
            duplicate_pairs={frozenset((records[0].source_id, records[1].source_id))},
        ),
    )
    selected = result.candidates

    assert len(selected) == 1
    assert result.funnel.input_records == 2
    assert result.funnel.semantic_duplicates_removed == 1
    assert result.funnel.candidate_pool == 1
    assert result.funnel.candidate_cap_removed == 0
    assert selected[0].duplicate_source_ids == tuple(
        f"abcd:6b8700ce67c6b37b062dd7a60abc76d7ef832a97:fixture:{value}"
        for value in (51, 52, 53)
    )


def test_stratify_adapter_identity_captures_similarity_thresholds() -> None:
    first = SklearnCosineStratifyAdapter(minimum_similarity=0.05)
    second = SklearnCosineStratifyAdapter(minimum_similarity=0.2)

    assert first.adapter_id != second.adapter_id
    assert "minimum_similarity=0.05" in first.adapter_id


def test_stratification_rejects_cap_that_cannot_cover_labels_and_buckets() -> None:
    records = scrub_abcd(
        [
            raw_record(60, "label-a"),
            raw_record(61, "label-b"),
            raw_record(62, "label-c"),
            raw_record(63, "label-c"),
        ]
    ).records
    assignments = tuple(
        ClusterAssignment(record.source_id, f"cluster:{record.subflow}", 1.0)
        for record in records
    )
    task_by_source = {
        records[0].source_id: "hard",
        records[1].source_id: "hard",
        records[2].source_id: "medium",
        records[3].source_id: "easy",
    }

    with pytest.raises(CandidateCapacityError, match="jointly cover"):
        stratify_candidates(
            records,
            assignments,
            (
                tau_summary("hard", "hard", 2),
                tau_summary("medium", "medium", 8),
                tau_summary("easy", "easy", 14),
            ),
            adapter=DeterministicFakeStratifyAdapter(task_by_source),
            target_count=3,
        )


def test_same_tau_task_does_not_merge_distinct_abcd_conversations() -> None:
    records = scrub_abcd(
        [
            {
                **raw_record(70, "return_size"),
                "original": [["customer", "different size request"]],
                "delexed": [
                    {
                        "speaker": "customer",
                        "text": "different size request",
                        "turn_count": 1,
                    }
                ],
            },
            {
                **raw_record(71, "return_size"),
                "original": [["customer", "damaged product request"]],
                "delexed": [
                    {
                        "speaker": "customer",
                        "text": "damaged product request",
                        "turn_count": 1,
                    }
                ],
            },
        ]
    ).records
    assignments = tuple(
        ClusterAssignment(record.source_id, "cluster:return", 1.0) for record in records
    )
    task_by_source = {record.source_id: "hard" for record in records}

    result = stratify_candidates(
        records,
        assignments,
        (tau_summary("hard", "hard", 2),),
        adapter=DeterministicFakeStratifyAdapter(task_by_source),
    )

    assert {item.source_id for item in result.candidates} == {
        record.source_id for record in records
    }
    assert result.funnel.semantic_duplicates_removed == 0
    assert {item.tau_task_id for item in result.candidates} == {"hard"}


def test_label_conflict_and_long_tail_keep_semantic_audit_information() -> None:
    records = scrub_abcd(
        [
            {
                **raw_record(80, "common"),
                "original": [["customer", "shared intent::common wording"]],
                "delexed": [
                    {
                        "speaker": "customer",
                        "text": "shared intent::common wording",
                        "turn_count": 1,
                    }
                ],
            },
            raw_record(81, "common"),
            raw_record(82, "common"),
            {
                **raw_record(83, "rare"),
                "original": [["customer", "shared intent::rare wording"]],
                "delexed": [
                    {
                        "speaker": "customer",
                        "text": "shared intent::rare wording",
                        "turn_count": 1,
                    }
                ],
            },
        ]
    ).records
    assignments = tuple(
        ClusterAssignment(record.source_id, "cluster:return", 1.0) for record in records
    )
    task_by_source = {record.source_id: None for record in records}

    result = stratify_candidates(
        records,
        assignments,
        (),
        adapter=DeterministicFakeStratifyAdapter(
            task_by_source,
            duplicate_pairs={frozenset((records[0].source_id, records[3].source_id))},
        ),
    )

    shared = [
        item
        for item in result.candidates
        if item.semantic_group_id
        == next(
            candidate.semantic_group_id
            for candidate in result.candidates
            if candidate.source_id == records[0].source_id
        )
    ]
    assert {item.source_id for item in shared} == {
        records[0].source_id,
        records[3].source_id,
    }
    assert all(item.label_conflict for item in shared)
    rare = next(item for item in shared if item.subflow == "rare")
    assert rare.long_tail is True


def test_tau_match_requires_a_finite_similarity() -> None:
    records = scrub_abcd([raw_record(90, "return_size")]).records
    assignments = (ClusterAssignment(records[0].source_id, "cluster:return", 1.0),)

    class InvalidTauSimilarityAdapter(DeterministicFakeStratifyAdapter):
        def match_tau(
            self,
            conversations: tuple[StratifyText, ...],
            tau_tasks: tuple[TauTaskText, ...],
            *,
            seed: int,
        ) -> tuple[TauTaskMatch, ...]:
            del tau_tasks, seed
            return (
                TauTaskMatch(
                    source_id=conversations[0].source_id,
                    tau_task_id="hard",
                    similarity=float("nan"),
                ),
            )

    with pytest.raises(StratifyContractError, match="must be finite"):
        stratify_candidates(
            records,
            assignments,
            (tau_summary("hard", "hard", 2),),
            adapter=InvalidTauSimilarityAdapter({records[0].source_id: "hard"}),
        )


def test_pairwise_seam_requires_the_complete_abcd_matrix() -> None:
    records = scrub_abcd(
        [
            raw_record(100, "return_size"),
            raw_record(101, "return_size"),
            raw_record(102, "return_size"),
        ]
    ).records
    assignments = tuple(
        ClusterAssignment(record.source_id, "cluster:return", 1.0) for record in records
    )

    class IncompletePairMatrixAdapter(DeterministicFakeStratifyAdapter):
        def compare_abcd(
            self,
            conversations: tuple[StratifyText, ...],
            *,
            seed: int,
        ) -> tuple[AbcdPairSimilarity, ...]:
            return super().compare_abcd(conversations, seed=seed)[:-1]

    with pytest.raises(StratifyContractError, match="missing ABCD pair similarities"):
        stratify_candidates(
            records,
            assignments,
            (),
            adapter=IncompletePairMatrixAdapter(
                {record.source_id: None for record in records}
            ),
        )


def test_tau_matching_runs_only_after_abcd_representative_selection() -> None:
    records = scrub_abcd(
        [
            raw_record(110, "return_size"),
            raw_record(111, "return_size"),
            raw_record(112, "return_size"),
        ]
    ).records
    assignments = tuple(
        ClusterAssignment(record.source_id, "cluster:return", 1.0) for record in records
    )
    all_pairs = {
        frozenset((left.source_id, right.source_id))
        for left_index, left in enumerate(records)
        for right in records[left_index + 1 :]
    }
    representative = min(records, key=lambda item: item.source_id)
    task_by_source = {
        record.source_id: (
            "hard" if record.source_id == representative.source_id else "easy"
        )
        for record in records
    }
    adapter = DeterministicFakeStratifyAdapter(
        task_by_source,
        duplicate_pairs=all_pairs,
    )

    result = stratify_candidates(
        records,
        assignments,
        (tau_summary("hard", "hard", 2), tau_summary("easy", "easy", 14)),
        adapter=adapter,
    )

    assert [item.source_id for item in adapter.seen_tau_conversations] == [
        representative.source_id
    ]
    assert len(result.candidates) == 1
    assert result.candidates[0].tau_task_id == "hard"
    assert result.candidates[0].duplicate_source_ids == tuple(
        sorted(record.source_id for record in records if record != representative)
    )
