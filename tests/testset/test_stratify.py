from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256

import pytest

from ses.testset.cluster import ClusterAssignment
from ses.testset.difficulty import TauDifficulty
from ses.testset.scrub import ScrubbedConversation, scrub_abcd
from ses.testset.stratify import (
    CandidateCapacityError,
    SklearnCosineStratifyAdapter,
    StratifyAnnotation,
    StratifyContractError,
    StratifyText,
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
        group_by_source: Mapping[str, str] | None = None,
    ) -> None:
        self.task_by_source = task_by_source
        self.group_by_source = group_by_source or {}
        self.seen_conversations: tuple[StratifyText, ...] = ()
        self.seen_tasks: tuple[TauTaskText, ...] = ()

    @property
    def adapter_id(self) -> str:
        return "deterministic-fake-stratify:v1"

    def annotate(
        self,
        conversations: tuple[StratifyText, ...],
        tau_tasks: tuple[TauTaskText, ...],
        *,
        seed: int,
    ) -> tuple[StratifyAnnotation, ...]:
        del seed
        self.seen_conversations = conversations
        self.seen_tasks = tau_tasks
        return tuple(
            StratifyAnnotation(
                source_id=item.source_id,
                semantic_group_id=self.group_by_source.get(
                    item.source_id, f"semantic:{item.source_id}"
                ),
                tau_task_id=self.task_by_source[item.source_id],
                similarity=1.0 if self.task_by_source[item.source_id] else None,
            )
            for item in reversed(conversations)
        )


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


def test_semantic_dedup_preserves_every_upstream_duplicate_id() -> None:
    records = scrub_abcd(
        [
            {
                **raw_record(50, "return_size"),
                "original": [["customer", "alpha"]],
                "delexed": [{"speaker": "customer", "text": "alpha", "turn_count": 1}],
            },
            {
                **raw_record(51, "return_size"),
                "original": [["customer", "alpha"]],
                "delexed": [{"speaker": "customer", "text": "alpha", "turn_count": 1}],
            },
            {
                **raw_record(52, "return_size"),
                "original": [["customer", "beta"]],
                "delexed": [{"speaker": "customer", "text": "beta", "turn_count": 1}],
            },
            {
                **raw_record(53, "return_size"),
                "original": [["customer", "beta"]],
                "delexed": [{"speaker": "customer", "text": "beta", "turn_count": 1}],
            },
        ]
    ).records
    assignments = tuple(
        ClusterAssignment(record.source_id, "cluster:return", 1.0) for record in records
    )
    task_by_source = {record.source_id: None for record in records}
    group_by_source = {record.source_id: "semantic:return" for record in records}

    result = stratify_candidates(
        records,
        assignments,
        (),
        adapter=DeterministicFakeStratifyAdapter(
            task_by_source, group_by_source=group_by_source
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


def test_semantic_group_rejects_conflicting_tau_difficulty_provenance() -> None:
    records = scrub_abcd(
        [raw_record(70, "return_size"), raw_record(71, "return_size")]
    ).records
    assignments = tuple(
        ClusterAssignment(record.source_id, "cluster:return", 1.0) for record in records
    )
    task_by_source = {
        records[0].source_id: "hard",
        records[1].source_id: "easy",
    }
    group_by_source = {record.source_id: "semantic:return" for record in records}

    with pytest.raises(StratifyContractError, match="conflicting tau task"):
        stratify_candidates(
            records,
            assignments,
            (tau_summary("hard", "hard", 2), tau_summary("easy", "easy", 14)),
            adapter=DeterministicFakeStratifyAdapter(
                task_by_source, group_by_source=group_by_source
            ),
        )


def test_semantic_group_rejects_tau_conflict_across_subflows() -> None:
    records = scrub_abcd(
        [raw_record(80, "return_size"), raw_record(81, "refund_status")]
    ).records
    assignments = tuple(
        ClusterAssignment(record.source_id, "cluster:return", 1.0) for record in records
    )
    task_by_source = {
        records[0].source_id: "hard",
        records[1].source_id: "easy",
    }
    group_by_source = {record.source_id: "semantic:return" for record in records}

    with pytest.raises(StratifyContractError, match="conflicting tau task"):
        stratify_candidates(
            records,
            assignments,
            (tau_summary("hard", "hard", 2), tau_summary("easy", "easy", 14)),
            adapter=DeterministicFakeStratifyAdapter(
                task_by_source, group_by_source=group_by_source
            ),
        )


def test_stratify_annotation_requires_similarity_for_a_tau_match() -> None:
    records = scrub_abcd([raw_record(90, "return_size")]).records
    assignments = (ClusterAssignment(records[0].source_id, "cluster:return", 1.0),)

    class MissingSimilarityAdapter:
        @property
        def adapter_id(self) -> str:
            return "missing-similarity-fake:v1"

        def annotate(
            self,
            conversations: tuple[StratifyText, ...],
            tau_tasks: tuple[TauTaskText, ...],
            *,
            seed: int,
        ) -> tuple[StratifyAnnotation, ...]:
            del tau_tasks, seed
            return (
                StratifyAnnotation(
                    source_id=conversations[0].source_id,
                    semantic_group_id="semantic:return",
                    tau_task_id="hard",
                    similarity=None,
                ),
            )

    with pytest.raises(StratifyContractError, match="both be present or absent"):
        stratify_candidates(
            records,
            assignments,
            (tau_summary("hard", "hard", 2),),
            adapter=MissingSimilarityAdapter(),
        )
